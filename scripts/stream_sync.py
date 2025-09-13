#!/usr/bin/env python3
"""
Cloudflare Stream sync for the animalsusingtools workflow.

What this script does (specialized)
- Verifies that all videos in <added_dir> are present on Stream
  (match by meta.project="added" and meta.source=<relative path under added_dir>)
- Uploads videos from <to_add_dir> using Stream's direct upload API and sets meta
  {name, project="added", source=<target relative path under added_dir>}
- After successful upload, moves the file into the corresponding subfolder under <added_dir>
- Appends upload details to logs/upload_log.csv

When to use this script
- You are following the to_add -> added workflow and want upload+move in one step
- You want a verification pass for what is already in added

Related scripts
- scripts/uploader.py: Generic folder uploader for Stream (videos) and Images (images)
- scripts/list_media.py: Audits and lists Stream/Images assets in CSV/JSON

Environment variables (or CLI flags) expected:
- CLOUDFLARE_ACCOUNT_ID
- CLOUDFLARE_API_TOKEN

CSV log format fields:
kind,project,local_path,id_or_uid,view_url,api_url,metadata_json,uploaded_at

Note:
- Uses Stream direct upload. For very large files, consider tus uploads.
"""

import argparse
import csv
import json
import logging
import mimetypes
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import coloredlogs
import requests
from uploader import CfUploader

API_BASE = "https://api.cloudflare.com/client/v4"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mpeg", ".mpg", ".m2ts"}
DEFAULT_MAX_DURATION_SECONDS = 6 * 60 * 60


@dataclass
class StreamVideo:
    uid: str
    meta: dict


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def iter_local_videos(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            # Skip macOS metadata files
            if p.name.startswith("._") or p.name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                continue
            # Skip typical NAS metadata dirs like @eaDir
            if any(
                parent.name in {"@eaDir", ".AppleDouble", ".Spotlight-V100", ".Trashes", ".fseventsd"}
                for parent in p.parents
            ):
                continue
            yield p


class StreamClient:
    def __init__(self, account_id: str, api_token: str):
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_token}"})
        self._rate_last_ts = 0.0
        self._min_interval = 1.0

    def _rate_limit(self):
        now = time.time()
        delta = now - self._rate_last_ts
        wait = self._min_interval - delta
        if wait > 0:
            time.sleep(wait)
        self._rate_last_ts = time.time()

    def list_all(self) -> list[StreamVideo]:
        videos: list[StreamVideo] = []
        page = 1
        per_page = 1000
        while True:
            url = f"{API_BASE}/accounts/{self.account_id}/stream"
            params = {"page": page, "per_page": per_page}
            self._rate_limit()
            resp = self.session.get(url, params=params, timeout=30)
            try:
                data = resp.json()
            except ValueError:
                data = {"raw": resp.text[:500]}
            if resp.status_code >= 400 or not data.get("success", True):
                raise RuntimeError(f"Stream list failed: HTTP {resp.status_code} {data}")
            items = data.get("result", []) or []
            for item in items:
                videos.append(StreamVideo(uid=item.get("uid", ""), meta=item.get("meta") or {}))
            result_info = data.get("result_info", {}) or {}
            count = result_info.get("count") or len(items)
            total_count = result_info.get("total_count")
            if count < per_page:
                break
            if total_count is not None and page * per_page >= int(total_count):
                break
            page += 1
        return videos

    def create_direct_upload(self, max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS) -> tuple[str, str]:
        url = f"{API_BASE}/accounts/{self.account_id}/stream/direct_upload"
        payload = {"maxDurationSeconds": max_duration_seconds}
        self._rate_limit()
        resp = self.session.post(url, json=payload, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(f"Create direct upload failed: HTTP {resp.status_code} {data}")
        result = data.get("result", {})
        return result["uploadURL"], result["uid"]

    def set_meta(self, uid: str, meta: dict) -> None:
        url = f"{API_BASE}/accounts/{self.account_id}/stream/{uid}"
        payload = {"meta": meta}
        self._rate_limit()
        resp = self.session.post(url, json=payload, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(f"Set meta failed for {uid}: HTTP {resp.status_code} {data}")


def load_upload_log(csv_path: Path) -> set[tuple[str, str, str]]:
    """Return set of (kind, project, local_path) from existing CSV if present."""
    existing: set[tuple[str, str, str]] = set()
    if not csv_path.exists():
        return existing
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            kind = (row.get("kind") or "").strip()
            project = (row.get("project") or "").strip()
            local_path = (row.get("local_path") or "").strip()
            if kind and project and local_path:
                existing.add((kind, project, local_path))
    return existing


def append_upload_log(
    csv_path: Path,
    *,
    kind: str,
    project: str,
    local_path: str,
    id_or_uid: str,
    view_url: str,
    api_url: str,
    metadata: dict,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if not csv_path.exists():
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "kind",
                    "project",
                    "local_path",
                    "id_or_uid",
                    "view_url",
                    "api_url",
                    "metadata_json",
                    "uploaded_at",
                ],
            )
            writer.writeheader()
    row = {
        "kind": kind,
        "project": project,
        "local_path": local_path,
        "id_or_uid": id_or_uid,
        "view_url": view_url,
        "api_url": api_url,
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "kind",
                "project",
                "local_path",
                "id_or_uid",
                "view_url",
                "api_url",
                "metadata_json",
                "uploaded_at",
            ],
        )
        writer.writerow(row)


def verify_added(stream: StreamClient, added_dir: Path, project_value: str = "added") -> list[tuple[Path, bool]]:
    """Return list of (path, exists_on_stream)."""
    existing = stream.list_all()
    # Build a fast lookup set of (project, source)
    existing_keys = set()
    for v in existing:
        meta = v.meta or {}
        existing_keys.add(((meta.get("project") or "").strip(), (meta.get("source") or "").strip()))

    results: list[tuple[Path, bool]] = []
    for p in iter_local_videos(added_dir):
        rel = str(p.relative_to(added_dir))
        key = (project_value, rel)
        results.append((p, key in existing_keys))
    return results


def upload_from_to_add(
    account_id: str,
    api_token: str,
    to_add_dir: Path,
    added_dir: Path,
    log_csv: Path,
    project_value: str = "added",
    dry_run: bool = False,
) -> list[tuple[Path, Optional[str]]]:
    """Upload all videos under to_add_dir via CfUploader and move to added_dir.

    Returns list of (original_path, uid). Skipped/failures have uid=None.
    """
    uploaded: list[tuple[Path, Optional[str]]] = []

    def on_uploaded(path: Path, rel: str, uid: str) -> None:
        dest = added_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(dest))
        except Exception as move_err:
            logging.error("Failed to move %s to %s: %s", path, dest, move_err)
        uploaded.append((path, uid))

    # Use CfUploader with project override and video-only filter
    CfUploader(
        account_id,
        api_token,
        concurrency=4,
        dry_run=dry_run,
        log_csv=log_csv,
        skip_existing=True,
        project_override=project_value,
        file_kinds={"video"},
        on_video_uploaded=on_uploaded,
    ).run(to_add_dir)

    # For dry run, we still report the files that would be processed
    if dry_run and not uploaded:
        for src in iter_local_videos(to_add_dir):
            uploaded.append((src, None))

    return uploaded


def main():
    def setup_logging(level: int) -> None:
        fmt = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d %(message)s"
        datefmt = "%H:%M:%S"
        coloredlogs.install(level=level, fmt=fmt, datefmt=datefmt)  # type: ignore
        logging.basicConfig(level=level, format=fmt, datefmt=datefmt)

    ap = argparse.ArgumentParser(
        description=("Verify and upload videos to Cloudflare Stream, preserving folder layout")
    )
    ap.add_argument("--account-id", default=os.getenv("CLOUDFLARE_ACCOUNT_ID"), help="Cloudflare Account ID")
    ap.add_argument("--api-token", default=os.getenv("CLOUDFLARE_API_TOKEN"), help="Cloudflare API Token")
    ap.add_argument(
        "--added-dir",
        type=Path,
        default=Path("/Users/julius/Library/CloudStorage/SynologyDrive-Mac/animalsusingtools/added"),
    )
    ap.add_argument(
        "--to-add-dir",
        type=Path,
        default=Path("/Users/julius/Library/CloudStorage/SynologyDrive-Mac/animalsusingtools/to_add"),
    )
    ap.add_argument(
        "--log-csv",
        type=Path,
        default=Path("/Users/julius/git/antools/logs/upload_log.csv"),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List actions but do not call the API or move files",
    )
    ap.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv)")
    ap.add_argument("--skip-verify", action="store_true", help="Skip the verify step for added dir")

    args = ap.parse_args()

    if not args.account_id or not args.api_token:
        print(
            ("Provide --account-id and --api-token or set " "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN"),
            file=sys.stderr,
        )
        sys.exit(2)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    setup_logging(level)

    stream = StreamClient(args.account_id, args.api_token)

    # Verify
    if not args.skip_verify:
        logging.info("Verifying videos in added dir exist on Stream...")
        checks = verify_added(stream, args.added_dir, project_value="added")
        missing = [p for p, ok in checks if not ok]
        if missing:
            print("These files are in 'added' but not found on Stream (by project+source):")
            for p in missing:
                print(f"- {p}")
        else:
            print("All videos in added dir are present on Stream.")

    # Upload from to_add
    logging.info("Uploading videos from to_add and moving them to added...")
    results = upload_from_to_add(
        args.account_id,
        args.api_token,
        args.to_add_dir,
        args.added_dir,
        args.log_csv,
        project_value="added",
        dry_run=args.dry_run,
    )
    uploaded = [(p, uid) for p, uid in results if uid]
    if uploaded:
        print(f"Uploaded {len(uploaded)} videos:")
        for p, uid in uploaded:
            print(f"- {p.name} (uid={uid})")
    else:
        print("No uploads performed.")


if __name__ == "__main__":
    main()
