import argparse
import contextlib
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

API_BASE = "https://api.cloudflare.com/client/v4"


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
                title = (meta.get("name") or v.get("name") or "").strip()
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
                filename = (img.get("filename") or "").strip()
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
    if out is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r.__dict__)


def write_json(rows: list[MediaRow], out: Path | None):
    payload = [r.__dict__ for r in rows]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out is None:
        print(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def main():
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
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")

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
