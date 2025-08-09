#!/usr/bin/env python3
import argparse
import concurrent.futures as futures
import logging
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mpeg", ".mpg", ".m2ts"}

DEFAULT_MAX_DURATION_SECONDS = 6 * 60 * 60  # reserve up to 6 hours per video; adjust if you like


class CfUploader:
    def __init__(self, account_id: str, api_token: str, concurrency: int = 4, dry_run: bool = False):
        self.account_id = account_id
        self.api_token = api_token
        self.concurrency = max(1, concurrency)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_token}"})
        self.dry_run = dry_run

    def run(self, folder: Path):
        files = [p for p in folder.rglob("*") if p.is_file()]
        if not files:
            logging.warning("No files found.")
            return

        work = []
        for p in files:
            kind = classify_file(p)
            if kind is None:
                logging.debug("Skipping (unknown type): %s", p)
                continue
            work.append((kind, p))

        logging.info(
            "Found %d uploadable files (%d images, %d videos).",
            len(work),
            sum(1 for k, _ in work if k == "image"),
            sum(1 for k, _ in work if k == "video"),
        )

        with futures.ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futs = [ex.submit(self._upload_one, kind, path) for kind, path in work]
            for f in futures.as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    logging.exception("Task failed: %s", e)

    def _upload_one(self, kind: str, path: Path):
        if self.dry_run:
            logging.info("[DRY RUN] Would upload %s as %s", path, kind)
            return

        if kind == "image":
            self._upload_image(path)
        elif kind == "video":
            self._upload_video_basic(path)
        else:
            logging.debug("Unknown kind for %s; skipping.", path)

    # ---------- Images ----------

    def _upload_image(self, path: Path):
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/images/v1"
        # You can add additional multipart fields if you want (e.g., "id", "metadata", "requireSignedURLs")
        files = {"file": (path.name, path.open("rb"), guess_mime(path))}
        # Example: send a custom id = file stem (optional; remove if you prefer UUID)
        data = {"id": path.stem}

        logging.info("Uploading image: %s", path)
        resp = self._retry(lambda: self.session.post(url, files=files, data=data, timeout=120))
        self._check(resp, f"Image upload failed for {path}")
        res = resp.json()
        image_id = res.get("result", {}).get("id")
        logging.info("✅ Image uploaded: %s (id=%s)", path.name, image_id)

    # ---------- Videos (Stream / basic direct-upload) ----------

    def _upload_video_basic(self, path: Path):
        size_bytes = path.stat().st_size
        # Basic direct-upload uses a one-time uploadURL; for very large files, consider tus (not implemented here).
        # If you routinely have >200 MB files, use tus uploads instead (Cloudflare docs).
        upload_url, uid = self._stream_create_direct_upload(DEFAULT_MAX_DURATION_SECONDS)
        logging.info("Uploading video: %s (uid=%s, size=%.2f MB)", path, uid, size_bytes / (1024 * 1024))

        files = {"file": (path.name, path.open("rb"), guess_mime(path))}
        resp = self._retry(lambda: requests.post(upload_url, files=files, timeout=600))
        if resp.status_code >= 400:
            logging.error("Video data upload failed for %s: %s %s", path, resp.status_code, resp.text[:500])
            raise RuntimeError(f"video data upload failed: {path}")

        # Optional: set the video's meta.name to the filename for easier searching.
        self._stream_set_name(uid, path.name)
        logging.info("✅ Video uploaded: %s (uid=%s)", path.name, uid)

    def _stream_create_direct_upload(self, max_duration_seconds: int) -> Tuple[str, str]:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/stream/direct_upload"
        payload = {"maxDurationSeconds": max_duration_seconds}
        resp = self._retry(lambda: self.session.post(url, json=payload, timeout=30))
        self._check(resp, "Creating Stream direct upload URL failed")
        data = resp.json().get("result", {})
        return data["uploadURL"], data["uid"]

    def _stream_set_name(self, uid: str, name: str):
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/stream/{uid}"
        payload = {"meta": {"name": name}}
        resp = self._retry(lambda: self.session.post(url, json=payload, timeout=30))
        self._check(resp, f"Setting Stream meta.name failed for uid={uid}")

    # ---------- utils ----------

    def _retry(self, fn, attempts: int = 5, base_sleep: float = 0.75):
        for i in range(attempts):
            try:
                resp = fn()
                # Retry on 429 and >=500
                if getattr(resp, "status_code", 200) in (429,) or getattr(resp, "status_code", 200) >= 500:
                    raise RuntimeError(f"HTTP {resp.status_code}: {getattr(resp, 'text', '')[:300]}")
                return resp
            except Exception as e:
                if i == attempts - 1:
                    raise
                sleep = base_sleep * (2**i) + (0.05 * i)
                logging.warning("Retry %d/%d after error: %s (sleep %.2fs)", i + 1, attempts, e, sleep)
                time.sleep(sleep)

    def _check(self, resp: requests.Response, msg: str):
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}
        if resp.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(f"{msg}: HTTP {resp.status_code} {data}")


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def classify_file(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    # Heuristic: try MIME if extension is missing/odd
    mime = guess_mime(path)
    if mime and mime.startswith("image/"):
        return "image"
    if mime and mime.startswith("video/"):
        return "video"
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Upload a folder of images to Cloudflare Images and videos to Cloudflare Stream."
    )
    ap.add_argument("folder", type=Path, help="Path to folder to upload")
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
    ap.add_argument("--concurrency", type=int, default=4, help="Parallel uploads (default 4)")
    ap.add_argument("--dry-run", action="store_true", help="List what would be uploaded, without uploading")
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
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")

    if not args.folder.exists() or not args.folder.is_dir():
        logging.error("Folder does not exist or is not a directory: %s", args.folder)
        sys.exit(1)

    CfUploader(args.account_id, args.api_token, concurrency=args.concurrency, dry_run=args.dry_run).run(args.folder)


if __name__ == "__main__":
    main()
