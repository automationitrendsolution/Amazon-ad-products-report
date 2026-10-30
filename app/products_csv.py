"""Reading, checking and merging the Products page exports.

One run exports one file per page (200 products each).  Before anything
reaches the database:

* each file is decoded with the first encoding that works, and any
  report-metadata lines before the header are skipped;
* every file must contain the twelve :data:`REQUIRED_COLUMNS`
  (only those are kept, exactly as the manual upload script did);
* blank rows and a "Total" summary row, if Amazon adds one, are dropped;
* a row may not appear on two pages (that would mean the export ignored
  the page and returned the same rows twice);
* the merged row count must equal the page's "of N results".
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import REQUIRED_COLUMNS
from .exceptions import CSVValidationError, IncompleteExportError
from .logging_config import get_logger

logger = get_logger("csv")

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_HEADER_SCAN_LINES = 20
#: Values Amazon writes for "no value".
NULL_TOKENS = frozenset({"", "-", "--", "—", "–", "n/a", "na", "null", "none", "nan"})
_SUMMARY_LABELS = frozenset({"total", "totals", "grand total"})
_NON_NUMERIC = re.compile(r"[^0-9eE.+-]")
_PARENTHESISED = re.compile(r"^\((.*)\)$")


# ---------------------------------------------------------------------------
# reading one export
# ---------------------------------------------------------------------------


def read_text(path: Path) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise CSVValidationError(f"Could not decode {path} with any of {_ENCODINGS}")


def detect_header(text: str, path: Path) -> tuple[int, str]:
    """Find the header row (Amazon may prefix metadata lines) and the delimiter."""
    lines = text.splitlines()[:_HEADER_SCAN_LINES]
    for index, line in enumerate(lines):
        delimiter = "\t" if line.count("\t") > line.count(",") else ","
        cells = {cell.strip() for cell in next(csv.reader([line], delimiter=delimiter), [])}
        if "Products" in cells and "Status" in cells:
            return index, delimiter
    first = lines[0][:150] if lines else ""
    raise CSVValidationError(
        f"{path.name}: no header row with 'Products' and 'Status' in the first "
        f"{_HEADER_SCAN_LINES} lines -- this is not a Products page export. First line: {first!r}"
    )


def read_export(path: Path) -> pd.DataFrame:
    """One page export -> the twelve required columns, as text.

    Raises:
        CSVValidationError: Unreadable, no header, or a required column is
            missing (it is not shown in the page's "Columns" picker).
    """
    text, _ = read_text(path)
    header_row, delimiter = detect_header(text, path)
    body = "\n".join(text.splitlines()[header_row:])
    try:
        frame = pd.read_csv(
            io.StringIO(body), sep=delimiter, dtype=str, keep_default_na=False, engine="python"
        )
    except Exception as exc:
        raise CSVValidationError(f"Could not read {path.name}: {exc}") from exc
    frame.columns = [str(c).strip() for c in frame.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise CSVValidationError(
            f"{path.name} is missing column(s): {', '.join(missing)}. Turn them on in the "
            "Products page's 'Columns' picker, which decides what Export writes."
        )
    frame = frame.loc[:, list(REQUIRED_COLUMNS)].apply(lambda s: s.str.strip())

    blank = frame.eq("").all(axis=1)
    summary = frame["Products"].str.lower().isin(_SUMMARY_LABELS)
    if bool(summary.any()):
        logger.info("%s: dropped %d summary row(s)", path.name, int(summary.sum()))
    return frame.loc[~blank & ~summary].reset_index(drop=True)


# ---------------------------------------------------------------------------
# merging the pages
# ---------------------------------------------------------------------------


@dataclass
class ProductsExport:
    """The merged, checked rows of one run."""

    frame: pd.DataFrame
    files: list[Path] = field(default_factory=list)
    rows_per_file: list[int] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.frame)


def combine_exports(paths: Sequence[Path], *, expected_total: int | None = None) -> ProductsExport:
    """Read every page export and merge them in page order.

    Args:
        expected_total: The page's "of N results"; the merged row count must
            equal it.  None skips the check (files loaded by hand).

    Raises:
        CSVValidationError: A file is unusable, or rows repeat across pages.
        IncompleteExportError: The merged rows do not add up to ``expected_total``.
    """
    if not paths:
        raise CSVValidationError("No export files to merge")
    frames = []
    for number, path in enumerate(paths, start=1):
        frame = read_export(path)
        logger.info("Page %d export %s: %d product row(s)", number, path.name, len(frame))
        frames.append(frame.assign(_page=number))
    merged = pd.concat(frames, ignore_index=True)

    key = list(REQUIRED_COLUMNS)
    repeated = merged.loc[merged.duplicated(subset=key, keep=False)]
    across_pages = repeated.groupby(key, dropna=False)["_page"].nunique()
    if bool((across_pages > 1).any()):
        raise CSVValidationError(
            f"{int((across_pages > 1).sum())} row(s) appear on more than one page export, e.g. "
            f"{across_pages[across_pages > 1].index[0][0]!r}. The export did not follow the page; "
            "nothing was loaded."
        )
    if not repeated.empty:
        logger.warning("%d identical row(s) within a single page export were kept", len(repeated))

    result = ProductsExport(
        frame=merged.drop(columns="_page"),
        files=list(paths),
        rows_per_file=[len(f) for f in frames],
    )
    if expected_total is not None and result.row_count != expected_total:
        raise IncompleteExportError(
            f"The page exports hold {result.row_count} row(s) "
            f"({' + '.join(map(str, result.rows_per_file))}), but the Products page shows "
            f"{expected_total} result(s). Nothing was loaded."
        )
    return result


# ---------------------------------------------------------------------------
# value cleaning, used when a destination column is numeric
# ---------------------------------------------------------------------------


def parse_number(value: object) -> float | None:
    """``$1,234.56`` / ``12.5%`` / ``(45.00)`` / ``—`` -> float or None.

    Raises:
        ValueError: A non-empty value that is not a number (e.g. ``Eligible``).
    """
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        return number if np.isfinite(number) else None
    text = str(value).strip()
    if text.lower() in NULL_TOKENS:
        return None
    negative = False
    match = _PARENTHESISED.match(text)
    if match:
        negative, text = True, match.group(1)
    cleaned = _NON_NUMERIC.sub("", text)
    if cleaned.startswith("-"):
        negative = True
    cleaned = cleaned.lstrip("+-")
    try:
        number = float(cleaned)
    except ValueError:
        raise ValueError(f"not a number: {value!r}") from None
    if not np.isfinite(number):
        raise ValueError(f"not a finite number: {value!r}")
    return -number if negative else number


def write_merged(export: ProductsExport, target: Path) -> Path:
    """Write the merged rows as one CSV (the file published to data/reports)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    export.frame.to_csv(target, index=False, encoding="utf-8")
    return target
