"""
Cloudflare media lister for Stream and Images.

What this script does
- Lists Cloudflare Stream videos and Cloudflare Images, including IDs, titles, project meta, timestamps
- Outputs CSV or JSON to stdout or a file
- Provides a --project filter that matches meta.project set during upload

When to use this script
- You want to quickly audit what exists in your Cloudflare account
- You need IDs to cross-reference or verify uploads

Related scripts
- scripts/uploader.py: Pushes local folders of media to Cloudflare
- scripts/stream_sync.py: Upload+move workflow tailored for the animalsusingtools added/to_add flow
"""

import argparse
import contextlib
import csv
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import coloredlogs
import requests

API_BASE = "https://api.cloudflare.com/client/v4"


# --- Helpers ---
def normalize_media_title(raw: str) -> str:
    """Convert things like 'dog_walk.mp3' to 'Dog Walk'.

    Steps:
    - strip extension
    - replace underscores/hyphens with spaces
    - collapse whitespace
    - title-case the result
    """
    if not raw:
        return ""

    # Remove extension if present
    stem = Path(raw).stem

    # Replace separators with spaces and normalize whitespace
    text = re.sub(r"[_\-]+", " ", stem)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    tokens = text.split(" ")

    # Title-case tokens
    titled = " ".join(tok.capitalize() for tok in tokens if tok)
    return fix_special_casing(titled)


def fix_special_casing(title: str) -> str:
    """Apply brand/style casing fixes (e.g., Ipad -> iPad, tv/Tv -> TV)."""
    replacements: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bipad\b", re.IGNORECASE), "iPad"),
        (re.compile(r"\btv\b", re.IGNORECASE), "TV"),
    ]
    fixed = title
    for pattern, repl in replacements:
        fixed = pattern.sub(repl, fixed)
    return fixed


@dataclass
class MediaRow:
    kind: str  # "video" or "image"
    id: str  # video uid or image id
    title: str  # video title (meta.name/name) or image filename
    project: str
    created: str  # ISO8601
    duration_seconds: Optional[float]


class CfMediaLister:
    def __init__(self, account_id: str, api_token: str):
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_token}"})

    def list_videos(self, project: Optional[str] = None, limit: Optional[int] = None) -> list[MediaRow]:
        rows: list[MediaRow] = []
        page = 1
        per_page = 1000  # Cloudflare API typically supports large page sizes
        total_fetched = 0

        while True:
            url = f"{API_BASE}/accounts/{self.account_id}/stream"
            params = {"page": page, "per_page": per_page}
            logging.debug("Fetching page %s", page)
            resp = self.session.get(url, params=params, timeout=30)
            try:
                data = resp.json()
            except ValueError:
                data = {"raw": resp.text[:500]}
            if resp.status_code >= 400 or not data.get("success", True):
                raise RuntimeError(f"Stream list failed: HTTP {resp.status_code} {data}")

            videos = data.get("result", []) or []
            result_info = data.get("result_info", {}) or {}
            logging.debug("Received %d videos on page %s", len(videos), page)

            for v in videos:
                uid = v.get("uid") or ""
                meta = v.get("meta") or {}
                # Our uploader sets meta.name, meta.project, meta.source
                raw_title = (meta.get("name") or v.get("name") or "").strip()
                title = normalize_media_title(raw_title)
                project_value = (meta.get("project") or "").strip()
                if project is not None and project_value != project:
                    continue
                created_raw = v.get("created") or ""
                created_iso = created_raw
                # Normalize to ISO8601 without modification; if parseable, reformat
                with contextlib.suppress(ValueError):
                    created_iso = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).isoformat()
                duration = v.get("duration")

                rows.append(
                    MediaRow(
                        kind="video",
                        id=uid,
                        title=title,
                        project=project_value,
                        created=created_iso,
                        duration_seconds=duration if isinstance(duration, (int, float)) else None,
                    )
                )
                total_fetched += 1
                if limit is not None and total_fetched >= limit:
                    return rows

            count = result_info.get("count") or len(videos)
            total_count = result_info.get("total_count")

            if count < per_page:
                break
            if total_count is not None and page * per_page >= int(total_count):
                break
            page += 1

        return rows

    def list_images(self, project: Optional[str] = None, limit: Optional[int] = None) -> list[MediaRow]:
        rows: list[MediaRow] = []
        page = 1
        per_page = 1000
        total_fetched = 0

        while True:
            url = f"{API_BASE}/accounts/{self.account_id}/images/v1"
            params = {"page": page, "per_page": per_page}
            logging.debug("Fetching images page %s", page)
            resp = self.session.get(url, params=params, timeout=30)
            try:
                data = resp.json()
            except ValueError:
                data = {"raw": resp.text[:500]}
            if resp.status_code >= 400 or not data.get("success", True):
                raise RuntimeError(f"Images list failed: HTTP {resp.status_code} {data}")

            raw_result = data.get("result", [])
            if isinstance(raw_result, dict):
                items = raw_result.get("images") or raw_result.get("items") or []
                result_info = raw_result.get("result_info") or data.get("result_info", {}) or {}
            else:
                items = raw_result or []
                result_info = data.get("result_info", {}) or {}

            logging.debug("Received %d images on page %s", len(items), page)

            for img in items:
                image_id = img.get("id") or ""
                filename = normalize_media_title((img.get("filename") or "").strip())
                meta = img.get("meta") or img.get("metadata") or {}
                project_value = (meta.get("project") or "").strip()
                if project is not None and project_value != project:
                    continue
                created_raw = img.get("uploaded") or ""
                created_iso = created_raw
                with contextlib.suppress(ValueError):
                    created_iso = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).isoformat()

                rows.append(
                    MediaRow(
                        kind="image",
                        id=image_id,
                        title=filename,
                        project=project_value,
                        created=created_iso,
                        duration_seconds=None,
                    )
                )
                total_fetched += 1
                if limit is not None and total_fetched >= limit:
                    return rows

            count = result_info.get("count") or len(items)
            total_count = result_info.get("total_count")

            if count < per_page:
                break
            if total_count is not None and page * per_page >= int(total_count):
                break
            page += 1

        return rows


def write_csv(rows: list[MediaRow], out: Path | None):
    fieldnames = ["kind", "id", "title", "project", "created", "duration_seconds"]

    videos = [r for r in rows if r.kind == "video"]
    images = [r for r in rows if r.kind == "image"]

    def _write(file_like):
        writer = csv.DictWriter(file_like, fieldnames=fieldnames)
        writer.writeheader()
        for r in videos:
            writer.writerow(r.__dict__)
        if videos and images:
            # Insert a blank line between sections
            file_like.write("\n")
        for r in images:
            writer.writerow(r.__dict__)

    if out is None:
        _write(sys.stdout)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            _write(f)


def write_json(rows: list[MediaRow], out: Path | None):
    payload = [r.__dict__ for r in rows]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out is None:
        print(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def main():
    def setup_logging(level: int) -> None:
        fmt = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d %(message)s"
        datefmt = "%H:%M:%S"
        coloredlogs.install(level=level, fmt=fmt, datefmt=datefmt)  # type: ignore
        logging.basicConfig(level=level, format=fmt, datefmt=datefmt)

    ap = argparse.ArgumentParser(description="List Cloudflare Stream videos and Cloudflare Images (titles and IDs)")
    ap.add_argument(
        "--account-id",
        default=os.getenv("CLOUDFLARE_ACCOUNT_ID"),
        required=False,
        help="Cloudflare Account ID (or set CLOUDFLARE_ACCOUNT_ID)",
    )
    ap.add_argument(
        "--api-token",
        default=os.getenv("CLOUDFLARE_API_TOKEN"),
        required=False,
        help="Cloudflare API Token (or set CLOUDFLARE_API_TOKEN)",
    )
    ap.add_argument("--project", default=None, help="Filter by meta.project value set during upload")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of items returned")
    ap.add_argument("--media", choices=["both", "videos", "images"], default="both", help="Which media to list")
    ap.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Output format (default csv)",
    )
    ap.add_argument("--out", type=Path, default=None, help="Optional output file; defaults to stdout")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="Increase log verbosity (-v, -vv)")

    args = ap.parse_args()

    if not args.account_id or not args.api_token:
        print(
            "You must provide --account-id and --api-token, or set CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(2)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    setup_logging(level)

    lister = CfMediaLister(args.account_id, args.api_token)

    rows: list[MediaRow] = []
    if args.media in ("both", "videos"):
        rows.extend(lister.list_videos(project=args.project, limit=None if args.media == "videos" else None))
    if args.media in ("both", "images"):
        rows.extend(lister.list_images(project=args.project, limit=None if args.media == "images" else None))

    if args.limit is not None:
        rows = rows[: args.limit]

    if args.format == "csv":
        write_csv(rows, args.out)
    else:
        write_json(rows, args.out)


if __name__ == "__main__":
    main()
