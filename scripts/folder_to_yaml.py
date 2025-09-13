#!/usr/bin/env python3
"""
Verify/upload a single folder's media to Cloudflare and print a YAML media section.

- Input: a directory path (typically under the 'added' root, e.g., .../added/electronics)
- Checks each image/video in that folder against Cloudflare (via CSV log and API)
  - If missing and not --dry-run, uploads it (Images or Stream as appropriate)
  - Uses meta.project='added' and meta.source=<relative path under the added root>
  - Appends to logs/upload_log.csv
- Prints a YAML block to stdout of the form:

media:
  - kind: video
    title: Example Title
    id: <uid-or-id>
    comment: ""

Environment variables (or flags): CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
"""

import argparse
import csv
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import coloredlogs
import requests

API_BASE = "https://api.cloudflare.com/client/v4"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mpeg", ".mpg", ".m2ts"}
DEFAULT_MAX_DURATION_SECONDS = 6 * 60 * 60


def setup_logging(level: int) -> None:
    fmt = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d %(message)s"
    datefmt = "%H:%M:%S"
    coloredlogs.install(level=level, fmt=fmt, datefmt=datefmt)  # type: ignore
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def classify_file(path: Path) -> Optional[str]:
    name = path.name
    if name.startswith("._") or name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
        return None
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    mime = guess_mime(path)
    if mime and mime.startswith("image/"):
        return "image"
    if mime and mime.startswith("video/"):
        return "video"
    return None


def should_ignore(path: Path) -> bool:
    if path.name.startswith("._"):
        return True
    if path.name in {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}:
        return True
    for parent in path.parents:
        if parent.name in {"@eaDir", ".AppleDouble", ".Spotlight-V100", ".Trashes", ".fseventsd"}:
            return True
    return False


def normalize_title(raw: str) -> str:
    stem = Path(raw).stem
    text = stem.replace("_", " ").replace("-", " ")
    text = " ".join(text.split()).strip()
    base = " ".join(tok.capitalize() for tok in text.split(" ") if tok)
    return fix_special_casing(base)


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


def stable_image_id(project: str, rel_path: str) -> str:
    digest = hashlib.sha1(f"{project}:{rel_path}".encode("utf-8")).hexdigest()
    return f"{project}-{digest}"


def load_csv_map(csv_path: Path) -> dict[tuple[str, str, str], dict]:
    mapping: dict[tuple[str, str, str], dict] = {}
    if not csv_path.exists():
        return mapping
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (
                (row.get("kind") or "").strip(),
                (row.get("project") or "").strip(),
                (row.get("local_path") or "").strip(),
            )
            if key[0] and key[1] and key[2]:
                mapping[key] = row
    return mapping


@dataclass
class StreamItem:
    uid: str
    meta: dict


def list_all_stream(account_id: str, token: str) -> list[StreamItem]:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    items: list[StreamItem] = []
    page, per_page = 1, 1000
    while True:
        url = f"{API_BASE}/accounts/{account_id}/stream"
        resp = session.get(url, params={"page": page, "per_page": per_page}, timeout=30)
        data = resp.json()
        if resp.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(f"Stream list failed: HTTP {resp.status_code} {data}")
        batch = data.get("result", []) or []
        for v in batch:
            items.append(StreamItem(uid=v.get("uid", ""), meta=v.get("meta") or {}))
        info = data.get("result_info", {}) or {}
        if (info.get("count") or len(batch)) < per_page:
            break
        page += 1
    return items


def image_exists(account_id: str, token: str, image_id: str) -> bool:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    url = f"{API_BASE}/accounts/{account_id}/images/v1/{image_id}"
    resp = session.get(url, timeout=15)
    return resp.status_code != 404


def upload_image(account_id: str, token: str, added_root: Path, path: Path, project: str, log_csv: Path) -> str:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    rel = str(path.relative_to(added_root))
    url = f"{API_BASE}/accounts/{account_id}/images/v1"
    meta = {"project": project, "source": rel}
    img_id = stable_image_id(project, rel)
    files = {"file": (path.name, path.open("rb"), guess_mime(path))}
    data = {"id": img_id, "metadata": json.dumps(meta)}
    logging.info("Uploading image: %s", rel)
    resp = session.post(url, files=files, data=data, timeout=120)
    data = resp.json()
    if resp.status_code >= 400 or not data.get("success", True):
        raise RuntimeError(f"Image upload failed: HTTP {resp.status_code} {data}")
    result_obj = data.get("result", {}) or {}
    view_url = result_obj.get("variants", [""])[0] if isinstance(result_obj, dict) else ""
    append_csv_row(
        log_csv,
        kind="image",
        project=project,
        local_path=rel,
        id_or_uid=img_id,
        view_url=view_url,
        api_url=f"{API_BASE}/accounts/{account_id}/images/v1/{img_id}",
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    time.sleep(0.5)
    return img_id


def create_direct_upload_url(account_id: str, token: str, max_duration: int) -> tuple[str, str]:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    url = f"{API_BASE}/accounts/{account_id}/stream/direct_upload"
    payload = {"maxDurationSeconds": max_duration}
    resp = session.post(url, json=payload, timeout=30)
    data = resp.json()
    if resp.status_code >= 400 or not data.get("success", True):
        raise RuntimeError(f"Create direct upload failed: HTTP {resp.status_code} {data}")
    result = data.get("result", {})
    return result["uploadURL"], result["uid"]


def set_stream_meta(account_id: str, token: str, uid: str, meta: dict) -> None:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    url = f"{API_BASE}/accounts/{account_id}/stream/{uid}"
    payload = {"meta": meta}
    resp = session.post(url, json=payload, timeout=30)
    data = resp.json()
    if resp.status_code >= 400 or not data.get("success", True):
        raise RuntimeError(f"Set meta failed for {uid}: HTTP {resp.status_code} {data}")


def append_csv_row(
    csv_path: Path,
    *,
    kind: str,
    project: str,
    local_path: str,
    id_or_uid: str,
    view_url: str,
    api_url: str,
    metadata_json: str,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "kind",
        "project",
        "local_path",
        "id_or_uid",
        "view_url",
        "api_url",
        "metadata_json",
        "uploaded_at",
    ]
    need_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if need_header:
            writer.writeheader()
        writer.writerow(
            {
                "kind": kind,
                "project": project,
                "local_path": local_path,
                "id_or_uid": id_or_uid,
                "view_url": view_url,
                "api_url": api_url,
                "metadata_json": metadata_json,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def main():
    ap = argparse.ArgumentParser(description="Check/upload a folder and output a YAML media block")
    ap.add_argument("folder", type=Path, help="Folder containing media (typically under 'added')")
    ap.add_argument("--account-id", default=os.getenv("CLOUDFLARE_ACCOUNT_ID"))
    ap.add_argument("--api-token", default=os.getenv("CLOUDFLARE_API_TOKEN"))
    ap.add_argument(
        "--added-root",
        type=Path,
        default=Path("/Users/julius/Library/CloudStorage/SynologyDrive-Mac/animalsusingtools/added"),
    )
    ap.add_argument("--log-csv", type=Path, default=Path("/Users/julius/git/antools/logs/upload_log.csv"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("--recursive", action="store_true", help="Recurse into subdirectories")

    args = ap.parse_args()
    if not args.account_id or not args.api_token:
        print("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN or pass flags", file=sys.stderr)
        sys.exit(2)

    level = logging.WARNING if args.verbose == 0 else (logging.INFO if args.verbose == 1 else logging.DEBUG)
    setup_logging(level)

    folder = args.folder.resolve()
    added_root = args.added_root.resolve()
    if not folder.exists() or not folder.is_dir():
        logging.error("Folder does not exist or is not a directory: %s", folder)
        sys.exit(1)
    try:
        rel_under_added = folder.relative_to(added_root)
    except Exception:
        logging.warning("Provided folder is not under the added root; meta.source may not match site conventions.")
        rel_under_added = None

    csv_map = load_csv_map(args.log_csv)

    # Build Stream index only if needed
    stream_index: dict[tuple[str, str], str] = {}

    # Collect candidate files
    def list_files(base: Path) -> list[Path]:
        if args.recursive:
            return [p for p in base.rglob("*") if p.is_file() and not should_ignore(p)]
        return [p for p in base.iterdir() if p.is_file() and not should_ignore(p)]

    files = [p for p in list_files(folder) if classify_file(p) in {"image", "video"}]
    files.sort(key=lambda p: p.name.lower())

    results: list[tuple[str, str, str]] = []  # (kind, title, id)
    needs_upload: list[tuple[str, str, str]] = []  # (kind, title, rel)

    for path in files:
        kind = classify_file(path)
        if not kind:
            continue
        # Determine rel path under added root for metadata/source consistency
        if rel_under_added is not None:
            rel = str(path.relative_to(added_root))
        else:
            rel = path.name

        title = normalize_title(path.name)

        # Check CSV first
        csv_key = (kind, "added", rel)
        id_or_uid: Optional[str] = None
        if csv_key in csv_map:
            id_or_uid = (csv_map[csv_key].get("id_or_uid") or "").strip()

        # If not found, verify on CF and upload if needed
        if id_or_uid is None:
            if kind == "image":
                img_id = stable_image_id("added", rel)
                exists = image_exists(args.account_id, args.api_token, img_id)
                if not exists and not args.dry_run:
                    img_id = upload_image(args.account_id, args.api_token, added_root, path, "added", args.log_csv)
                id_or_uid = img_id
            else:  # video
                if not stream_index:
                    logging.info("Listing Stream videos to build index ...")
                    for item in list_all_stream(args.account_id, args.api_token):
                        meta = item.meta or {}
                        key = ((meta.get("project") or "").strip(), (meta.get("source") or "").strip())
                        if key[0] and key[1]:
                            stream_index[key] = item.uid
                uid = stream_index.get(("added", rel))
                if not uid and not args.dry_run:
                    # Direct upload to Stream
                    upload_url, uid = create_direct_upload_url(
                        args.account_id,
                        args.api_token,
                        DEFAULT_MAX_DURATION_SECONDS,
                    )
                    logging.info("Uploading video: %s (uid=%s)", rel, uid)
                    files_payload = {"file": (path.name, path.open("rb"), guess_mime(path))}
                    resp = requests.post(upload_url, files=files_payload, timeout=600)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Video upload failed: HTTP {resp.status_code} {resp.text[:300]}")
                    set_stream_meta(
                        args.account_id,
                        args.api_token,
                        uid,
                        {"name": path.name, "project": "added", "source": rel},
                    )
                    append_csv_row(
                        args.log_csv,
                        kind="video",
                        project="added",
                        local_path=rel,
                        id_or_uid=uid,
                        view_url=f"https://watch.cloudflarestream.com/{uid}",
                        api_url=(f"{API_BASE}/accounts/{args.account_id}/stream/{uid}"),
                        metadata_json=json.dumps(
                            {"name": path.name, "project": "added", "source": rel},
                            ensure_ascii=False,
                        ),
                    )
                    # Update index so subsequent loops see it
                    stream_index[("added", rel)] = uid
                    time.sleep(0.5)
                id_or_uid = uid or ""

        if not (id_or_uid or "").strip():
            needs_upload.append((kind, title, rel))
        results.append((kind, title, id_or_uid or ""))

    # Print summary of items that still need upload (useful in dry-run)
    if needs_upload:
        print("Needs upload (not found on Cloudflare):")
        for k, t, r in needs_upload:
            print(f"- {k}: {t} [{r}]")
        print("")

    # Print YAML media section
    print("media:")
    for kind, title, id_or_uid in results:
        print(f"  - kind: {kind}")
        print(f"    title: {title}")
        print(f"    id: {id_or_uid}")
        print("    comment: \"\"")


if __name__ == "__main__":
    main()
