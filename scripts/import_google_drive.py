#!/usr/bin/env python3
"""Download every public PDF exposed by a Google Drive embedded folder view."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import bs4
import gdown
import requests

EMBEDDED_FOLDER_URL = "https://drive.google.com/embeddedfolderview?id={folder_id}#list"
FILE_URL_PATTERN = re.compile(r"https://drive\.google\.com/file/d/([^/]+)/")
GITHUB_FILE_LIMIT = 100 * 1024 * 1024


@dataclass(frozen=True)
class DriveFile:
    file_id: str
    name: str


def normalise_filename(value: str, file_id: str) -> str:
    """Keep the Drive name while making path traversal impossible."""
    value = html.unescape(value)
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace("/", "-").replace("\\", "-").replace("\x00", "")
    value = value.removeprefix("PDF ").strip()
    if not value:
        value = f"{file_id}.pdf"
    return value


def list_public_files(folder_id: str) -> list[DriveFile]:
    url = EMBEDDED_FOLDER_URL.format(folder_id=folder_id)
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
            )
        },
        timeout=90,
    )
    response.raise_for_status()

    soup = bs4.BeautifulSoup(response.text, "html.parser")
    found: dict[str, DriveFile] = {}
    for anchor in soup.select('a[href*="drive.google.com/file/d/"]'):
        match = FILE_URL_PATTERN.search(anchor.get("href", ""))
        if not match:
            continue
        file_id = match.group(1)
        text = anchor.get_text(" ", strip=True)
        # Each Drive item normally has a thumbnail link and a separate named
        # link. Ignore the empty thumbnail and retain the named one.
        if not text:
            continue
        found[file_id] = DriveFile(
            file_id=file_id,
            name=normalise_filename(text, file_id),
        )

    files = sorted(found.values(), key=lambda item: item.name.casefold())
    if not files:
        raise RuntimeError("Google Drive did not expose any downloadable files")
    print(f"Discovered {len(files)} files in public folder {folder_id}", flush=True)
    return files


def unique_targets(files: list[DriveFile], output_dir: Path) -> list[tuple[DriveFile, Path]]:
    used: set[str] = set()
    targets: list[tuple[DriveFile, Path]] = []
    for item in files:
        name = item.name
        folded = name.casefold()
        if folded in used:
            source = Path(name)
            name = f"{source.stem} ({item.file_id}){source.suffix}"
            folded = name.casefold()
        used.add(folded)
        targets.append((item, output_dir / name))
    return targets


def is_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(5) == b"%PDF-"
    except OSError:
        return False


def download_one(item: DriveFile, target: Path, attempts: int = 5) -> None:
    for attempt in range(1, attempts + 1):
        print(f"[{attempt}/{attempts}] {item.name}", flush=True)
        try:
            result = gdown.download(
                id=item.file_id,
                output=str(target),
                quiet=True,
                use_cookies=True,
                resume=True,
            )
            if result and target.is_file() and target.stat().st_size and is_pdf(target):
                if target.stat().st_size >= GITHUB_FILE_LIMIT:
                    raise RuntimeError(
                        f"{item.name} is too large for regular GitHub storage "
                        f"({target.stat().st_size} bytes)"
                    )
                return
        except Exception as error:  # Retry transient Drive failures.
            print(f"Download error: {error}", file=sys.stderr, flush=True)
        if attempt < attempts:
            time.sleep(min(5 * attempt, 20))
    raise RuntimeError(f"Could not download a valid PDF: {item.name} ({item.file_id})")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_indexes(
    downloaded: list[tuple[DriveFile, Path]],
    folder_id: str,
    manifest_path: Path,
    catalogue_path: Path,
) -> None:
    records = []
    for item, path in downloaded:
        records.append((item, path, path.stat().st_size, sha256(path)))

    with manifest_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["google_drive_id", "filename", "size_bytes", "sha256"])
        for item, path, size, checksum in records:
            writer.writerow([item.file_id, path.name, size, checksum])

    total_size = sum(record[2] for record in records)
    source_url = f"https://drive.google.com/drive/folders/{folder_id}"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Danh mục tài liệu",
        "",
        f"- **Nguồn:** [Google Drive]({source_url})",
        f"- **Số tài liệu:** {len(records)}",
        f"- **Tổng dung lượng:** {total_size / (1024 * 1024):,.1f} MiB",
        f"- **Cập nhật:** {generated_at}",
        "",
        "Mã Google Drive, kích thước chính xác và SHA-256 được lưu trong "
        "[`tai-lieu-manifest.tsv`](tai-lieu-manifest.tsv).",
        "",
        "## Tệp PDF",
        "",
    ]
    for _item, path, size, _checksum in records:
        encoded_name = quote(path.name)
        lines.append(f"- [{path.name}](tai-lieu/{encoded_name}) — {size / (1024 * 1024):,.1f} MiB")
    lines.append("")
    catalogue_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder-id", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("tai-lieu"))
    parser.add_argument("--manifest", type=Path, default=Path("tai-lieu-manifest.tsv"))
    parser.add_argument("--catalogue", type=Path, default=Path("DANH_MUC_TAI_LIEU.md"))
    parser.add_argument(
        "--minimum-files",
        type=int,
        default=50,
        help="Fail if Drive returns fewer files, guarding against a partial listing",
    )
    args = parser.parse_args()

    files = list_public_files(args.folder_id)
    if len(files) < args.minimum_files:
        raise RuntimeError(
            f"Only {len(files)} files were listed; expected at least {args.minimum_files}"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    targets = unique_targets(files, args.output_dir)
    for item, target in targets:
        if target.is_file() and is_pdf(target):
            print(f"Skipping existing PDF: {target.name}", flush=True)
            continue
        download_one(item, target)

    write_indexes(targets, args.folder_id, args.manifest, args.catalogue)
    print(
        f"Finished: {len(targets)} PDFs, "
        f"{sum(path.stat().st_size for _, path in targets)} bytes",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
