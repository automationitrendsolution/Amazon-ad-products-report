"""Validation and naming of downloaded export files.

The download itself is captured in :mod:`app.products_page` through
Playwright's download event, so a file is only accepted once Chromium reports
the transfer finished -- never a partial ``.crdownload``.
"""

from __future__ import annotations

import gzip
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .exceptions import CSVValidationError
from .logging_config import get_logger
from .timeutils import now_utc

logger = get_logger("downloader")

#: Anything smaller than this is a truncated or error response, not an export.
MIN_FILE_BYTES = 16
#: Leading bytes that mean Amazon served a web page instead of a CSV.
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml")
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"  # .xlsx is a zip container
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class DownloadResult:
    """A validated file on local disk."""

    path: Path
    size_bytes: int
    suggested_filename: str

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024


def target_filename(suggested: str, label: str) -> str:
    """``<UTC stamp>_<label>_<name>.csv`` -- unique and housekeeping-owned."""
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe = _SAFE_NAME.sub("_", Path(suggested).name).strip("_") or "Products.csv"
    if safe.lower().endswith(".gz"):
        safe = safe[:-3]
    if not safe.lower().endswith(".csv"):
        safe = f"{Path(safe).stem}.csv"
    return f"{stamp}_{_SAFE_NAME.sub('-', label)}_{safe}"


def _gunzip_in_place(path: Path) -> None:
    """Amazon sometimes serves export files gzip-compressed."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(path, "rb") as source, tmp.open("wb") as target:
        shutil.copyfileobj(source, target)
    tmp.replace(path)
    logger.info("Decompressed gzip export %s", path.name)


def validate_download(path: Path) -> int:
    """Check that ``path`` exists, is non-empty and is CSV text; return its size.

    Raises:
        CSVValidationError: If the file is missing, empty, a spreadsheet, an
            HTML page, or has no delimited header row.
    """
    if not path.is_file():
        raise CSVValidationError(f"Downloaded file does not exist: {path}")

    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic.startswith(_GZIP_MAGIC):
        _gunzip_in_place(path)
    elif magic.startswith(_ZIP_MAGIC):
        raise CSVValidationError(
            f"Downloaded file {path.name} is an Excel/zip file, not CSV. Choose the CSV "
            "export on the Products page (config/selectors.json 'export_menu_item')."
        )

    size = path.stat().st_size
    if size < MIN_FILE_BYTES:
        raise CSVValidationError(
            f"Downloaded file {path} is {size} bytes -- too small to be a Products export"
        )

    with path.open("rb") as handle:
        head = handle.read(4096)

    if head.lstrip().lower().startswith(_HTML_PREFIXES):
        raise CSVValidationError(
            f"Downloaded file {path} looks like an HTML page, not a CSV export. "
            "This usually means the session expired mid-download."
        )

    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = head.decode("latin-1")

    if "," not in text and "\t" not in text:
        first_line = text.splitlines()[0] if text.splitlines() else ""
        raise CSVValidationError(
            f"Downloaded file {path} has no delimited header row: {first_line[:120]!r}"
        )

    logger.info("Download validated: %s (%.1f KB)", path, size / 1024)
    return size
