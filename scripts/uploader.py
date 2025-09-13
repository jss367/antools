"""
For unknown reasons, CloudFlare keeps saying I've used up all my quota and only uploads a few videos at a time.

Running this file over and over eventually uploads all the videos.


Cloudflare uploader for images and videos.

What this script does
- Uploads images to Cloudflare Images and videos to Cloudflare Stream
- Sets metadata for each asset: meta.project = <root folder name>, meta.source = <relative path>
- Supports concurrency, retries, and simple rate limiting
- Can append upload details to logs/upload_log.csv (IDs, URLs, metadata)
- Optional skip-existing behavior based on the CSV and Cloudflare IDs (for images)

When to use this script
- You want a generic one-way upload for an arbitrary folder tree
- You are okay with meta.project being the folder name you pass in
- You do not need to move files after upload; just publish them

Related scripts
- scripts/list_media.py: Lists what already exists on Cloudflare (Stream + Images)
- scripts/stream_sync.py: Specialized workflow that uploads videos from "to_add",
  tags them as project "added", and then moves them into the matching path under
  the "added" folder after successful upload. Use that if you want upload+move.
"""

import argparse
import concurrent.futures as futures
import csv
import hashlib
import json
import logging
import mimetypes
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Set, Tuple

import coloredlogs
import requests


def setup_logging(level: int) -> None:
    """Install colored logging

    The format includes timestamp, level, logger name, line number, and message.
    """
    fmt = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d %(message)s"
    datefmt = "%H:%M:%S"
    coloredlogs.install(level=level, fmt=fmt, datefmt=datefmt)  # type: ignore
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)


class QuotaExceededError(Exception):
    pass


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mpeg", ".mpg", ".m2ts"}

DEFAULT_MAX_DURATION_SECONDS = 6 * 60 * 60  # reserve up to 6 hours per video


class CfUploader:
    def __init__(
        self,
        account_id: str,
        api_token: str,
        concurrency: int = 4,
        dry_run: bool = False,
        log_csv: Optional[Path] = None,
        skip_existing: bool = False,
        project_override: Optional[str] = None,
        file_kinds: Optional[Set[str]] = None,  # e.g., {"video"}, {"image"}, or None for both
        on_video_uploaded: Optional[Callable[[Path, str, str], None]] = None,  # (path, rel, uid)
    ):
        self.account_id = account_id
        self.api_token = api_token
        self.concurrency = max(1, concurrency)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_token}"})
        self.dry_run = dry_run
        self.log_csv_path = log_csv
        self.csv_lock = threading.Lock()
        self.root_folder: Optional[Path] = None
        self.project: Optional[str] = None
        self.skip_existing = skip_existing
        self._existing_key_set: set[tuple[str, str, str]] = set()
        self.stream_quota_exceeded = threading.Event()
        # Global rate limit between Stream API calls (seconds)
        self.stream_min_interval_seconds: float = 1.0
        self._stream_rate_lock = threading.Lock()
        self._last_stream_call_ts: float = 0.0
        self.project_override = project_override
        self.file_kinds: Optional[Set[str]] = file_kinds
        self.on_video_uploaded = on_video_uploaded

    def run(self, folder: Path):
        self.root_folder = folder.resolve()
        self.project = self.project_override or self.root_folder.name

        # Prepare CSV if requested
        if self.log_csv_path is not None:
            csv_path = self.log_csv_path
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
            # Load existing records for skip logic
            if self.skip_existing and csv_path.exists():
                try:
                    with csv_path.open("r", newline="", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            kind = (row.get("kind") or "").strip()
                            project = (row.get("project") or "").strip()
                            local_path = (row.get("local_path") or "").strip()
                            if kind and project and local_path:
                                self._existing_key_set.add((kind, project, local_path))
                    logging.info(
                        "Loaded %d existing records from CSV for skip checks",
                        len(self._existing_key_set),
                    )
                except Exception as e:
                    logging.warning("Could not load existing CSV for skip logic: %s", e)

        files = [p for p in folder.rglob("*") if p.is_file() and not should_ignore_file(p)]
        if not files:
            logging.warning("No files found.")
            return

        work = []
        for p in files:
            kind = classify_file(p)
            if kind is None:
                logging.debug("Skipping (unknown type): %s", p)
                continue
            if self.file_kinds is not None and kind not in self.file_kinds:
                logging.debug("Skipping (filtered kind %s): %s", kind, p)
                continue
            work.append((kind, p))

        logging.info(
            "Found %d uploadable files (%d images, %d videos).",
            len(work),
            sum(k == "image" for k, _ in work),
            sum(k == "video" for k, _ in work),
        )

        with futures.ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futs = [ex.submit(self._upload_one, kind, path) for kind, path in work]
            for f in futures.as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    logging.exception("Task failed: %s", e)

    def _upload_one(self, kind: str, path: Path):
        rel = str(path.relative_to(self.root_folder)) if self.root_folder else path.name
        if self.dry_run:
            if self.skip_existing and (kind, self.project or "", rel) in getattr(self, "_existing_key_set", set()):
                logging.info("[DRY RUN] Would skip existing %s (CSV): %s", kind, rel)
                return
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
        rel = str(path.relative_to(self.root_folder)) if self.root_folder else path.name
        metadata = {"project": self.project or "", "source": rel}
        # Use a stable image id based on project and relative path
        stable_id = self._stable_image_id(rel)

        # Skip checks
        if self.skip_existing and ("image", self.project or "", rel) in self._existing_key_set:
            logging.info("Skipping existing image (CSV): %s", rel)
            return
        if self.skip_existing:
            try:
                if self._image_exists_by_id(stable_id):
                    logging.info("Skipping existing image (Cloudflare): %s", rel)
                    return
            except Exception as e:
                logging.debug("Image existence check failed for %s: %s", rel, e)

        data = {"id": stable_id, "metadata": json.dumps(metadata)}

        logging.info("Uploading image: %s", path)
        resp = self._retry(lambda: self.session.post(url, files=files, data=data, timeout=120))
        self._check(resp, f"Image upload failed for {path}")
        res = resp.json()
        result = res.get("result", {})
        image_id = result.get("id")
        variants = result.get("variants") or []
        view_url = variants[0] if variants else ""
        api_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/images/v1/{image_id}" if image_id else ""
        )
        logging.info("✅ Image uploaded: %s (id=%s)", path.name, image_id)

        # CSV log
        if not self.dry_run and self.log_csv_path is not None:
            self._append_csv_row(
                kind="image",
                project=self.project or "",
                local_path=rel,
                id_or_uid=image_id or "",
                view_url=view_url,
                api_url=api_url,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            if self.skip_existing:
                self._existing_key_set.add(("image", self.project or "", rel))
        time.sleep(1)

    # ---------- Videos (Stream / basic direct-upload) ----------

    def _upload_video_basic(self, path: Path):
        rel = str(path.relative_to(self.root_folder)) if self.root_folder else path.name
        # Skip based on CSV for videos
        if self.skip_existing and ("video", self.project or "", rel) in self._existing_key_set:
            logging.info("Skipping existing video (CSV): %s", rel)
            return

        size_bytes = path.stat().st_size
        # Basic direct-upload uses a one-time uploadURL; for very large files, consider tus (not implemented here).
        # If you routinely have >200 MB files, use tus uploads instead (Cloudflare docs).
        # Rate-limit requests for direct upload URLs across threads
        self._rate_limit_stream()
        try:
            upload_url, uid = self._stream_create_direct_upload(DEFAULT_MAX_DURATION_SECONDS)
        except QuotaExceededError:
            logging.warning("Cloudflare Stream quota exceeded (10011). Waiting 1s before continuing to next video.")
            time.sleep(1)
            return
        logging.info("Uploading video: %s (uid=%s, size=%.2f MB)", path, uid, size_bytes / (1024 * 1024))

        files = {"file": (path.name, path.open("rb"), guess_mime(path))}
        resp = self._retry(lambda: requests.post(upload_url, files=files, timeout=600))
        if resp.status_code >= 400:
            logging.error("Video data upload failed for %s: %s %s", path, resp.status_code, resp.text[:500])
            raise RuntimeError(f"video data upload failed: {path}")

        # Set Stream metadata including project and source
        self._stream_set_meta(uid, {"name": path.name, "project": self.project or "", "source": rel})
        info = None
        try:
            info = self._stream_get_info(uid)
        except Exception as e:
            logging.warning("Could not fetch Stream info for uid=%s: %s", uid, e)
        logging.info("✅ Video uploaded: %s (uid=%s)", path.name, uid)

        # CSV log
        if not self.dry_run and self.log_csv_path is not None:
            view_url = f"https://watch.cloudflarestream.com/{uid}"
            api_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/stream/{uid}"
            try:
                playback = (info or {}).get("result", {}).get("playback", {})
                if isinstance(playback, dict) and playback.get("hls"):
                    view_url = playback.get("hls")
            except Exception:
                pass
            self._append_csv_row(
                kind="video",
                project=self.project or "",
                local_path=rel,
                id_or_uid=uid or "",
                view_url=view_url,
                api_url=api_url,
                metadata_json=json.dumps(
                    {"project": self.project or "", "source": rel, "name": path.name},
                    ensure_ascii=False,
                ),
            )
            if self.skip_existing:
                self._existing_key_set.add(("video", self.project or "", rel))
        time.sleep(1)
        # Post-upload callback (e.g., move files)
        if self.on_video_uploaded is not None:
            try:
                self.on_video_uploaded(path, rel, uid)
            except Exception as e:
                logging.warning("on_video_uploaded callback failed for %s: %s", path, e)

    def _rate_limit_stream(self) -> None:
        """Ensure a minimum interval between Stream API calls across threads."""
        if self.stream_min_interval_seconds <= 0:
            return
        with self._stream_rate_lock:
            now = time.time()
            elapsed = now - self._last_stream_call_ts
            wait = self.stream_min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            self._last_stream_call_ts = now

    def _stream_create_direct_upload(self, max_duration_seconds: int) -> Tuple[str, str]:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/stream/direct_upload"
        payload = {"maxDurationSeconds": max_duration_seconds}
        resp = self._retry(lambda: self.session.post(url, json=payload, timeout=30))
        # Detect quota exceeded to short-circuit further video uploads
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code == 413 and any(
            isinstance(err, dict) and err.get("code") == 10011 for err in (data.get("errors") or [])
        ):
            raise QuotaExceededError("Cloudflare Stream storage capacity exceeded")
        self._check(resp, "Creating Stream direct upload URL failed")
        data = resp.json().get("result", {})
        return data["uploadURL"], data["uid"]

    def _stream_set_meta(self, uid: str, meta: dict):
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/stream/{uid}"
        payload = {"meta": meta}
        resp = self._retry(lambda: self.session.post(url, json=payload, timeout=30))
        self._check(resp, f"Setting Stream meta failed for uid={uid}")

    def _stream_get_info(self, uid: str) -> dict:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/stream/{uid}"
        resp = self._retry(lambda: self.session.get(url, timeout=30))
        self._check(resp, f"Fetching Stream info failed for uid={uid}")
        return resp.json()

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

    # ----- Helpers -----
    def _stable_image_id(self, rel_path: str) -> str:
        project = self.project or ""
        digest = hashlib.sha1(f"{project}:{rel_path}".encode("utf-8")).hexdigest()
        return f"{project}-{digest}"

    def _image_exists_by_id(self, image_id: str) -> bool:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/images/v1/{image_id}"
        resp = self._retry(lambda: self.session.get(url, timeout=15))
        if resp.status_code == 404:
            return False
        self._check(resp, f"Checking image id existence failed for id={image_id}")
        return True

    # ----- CSV logging -----
    def _append_csv_row(
        self,
        *,
        kind: str,
        project: str,
        local_path: str,
        id_or_uid: str,
        view_url: str,
        api_url: str,
        metadata_json: str,
    ) -> None:
        if self.log_csv_path is None:
            return
        row = {
            "kind": kind,
            "project": project,
            "local_path": local_path,
            "id_or_uid": id_or_uid,
            "view_url": view_url,
            "api_url": api_url,
            "metadata_json": metadata_json,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.csv_lock:
            with self.log_csv_path.open("a", newline="", encoding="utf-8") as f:
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


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def classify_file(path: Path) -> Optional[str]:
    # Ignore AppleDouble sidecar files and hidden dotfiles
    name = path.name
    if name.startswith("._") or name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
        return None
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


def should_ignore_file(path: Path) -> bool:
    """Return True for macOS/Windows/NAS metadata files we don't want to upload."""
    name = path.name
    if name.startswith("._"):
        return True
    if name.startswith(".") and name not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".bmp",
        ".tiff",
        ".tif",
        ".heic",
        ".heif",
        ".mp4",
        ".mov",
        ".m4v",
        ".mkv",
        ".webm",
        ".avi",
        ".wmv",
        ".mpeg",
        ".mpg",
        ".m2ts",
    }:
        return True
    if name in {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}:
        return True
    # Ignore common NAS/cloud metadata directories like Synology's @eaDir
    ignored_dirs = {"@eaDir", ".AppleDouble", ".Spotlight-V100", ".Trashes", ".fseventsd"}
    for parent in path.parents:
        if parent.name in ignored_dirs:
            return True
    return False


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
    ap.add_argument(
        "--log-csv",
        type=Path,
        default=None,
        help="Optional path to CSV file to append upload records (IDs, URLs, metadata)",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist (based on CSV records; images also verified by Cloudflare id)",
    )
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

    if not args.folder.exists() or not args.folder.is_dir():
        logging.error("Folder does not exist or is not a directory: %s", args.folder)
        sys.exit(1)

    CfUploader(
        args.account_id,
        args.api_token,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        log_csv=args.log_csv,
        skip_existing=args.skip_existing,
    ).run(args.folder)


if __name__ == "__main__":
    main()
