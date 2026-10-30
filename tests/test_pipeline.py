"""The whole run with the browser replaced by prepared page exports."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.downloader import DownloadResult
from app.exceptions import IncompleteExportError
from app.main import ProductsReportPipeline, RunMode, RunOptions
from app.products_csv import combine_exports
from app.products_page import PagedExport

from .conftest import export_text, make_products_table, row, write

#: 14:00 IST on Oct 29 = 01:30 Oct 29 in Los Angeles.
AT_2PM_IST = datetime(2026, 10, 29, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def fake_export(settings, pages, total):
    def export(period):
        files = []
        for number, rows in enumerate(pages, start=1):
            path = write(settings.download_dir / f"20261029T083000Z_{period.period}-p{number:02d}_Products.csv",
                         export_text(rows))
            files.append(DownloadResult(path, path.stat().st_size, "Products.csv"))
        return PagedExport(files=files, total=total)

    return export


def pipeline(settings, table, export_fn, *, period="yesterday", dry_run=False):
    options = RunOptions(mode=RunMode.LOAD_PRODUCTS, period=period, dry_run=dry_run)
    return ProductsReportPipeline(
        settings, options, database_factory=lambda: table, export_fn=export_fn, reference_time=AT_2PM_IST
    )


def test_yesterday_run_loads_every_page_under_yesterdays_date(settings):
    table = make_products_table()
    export = fake_export(settings, [[row("A"), row("B")], [row("C")]], total=3)
    summary = pipeline(settings, table, export).run()

    assert summary.succeeded
    assert summary.period.report_date == date(2026, 10, 28)
    assert (summary.write.deleted, summary.write.inserted) == (0, 3)
    assert table.count_for_date(date(2026, 10, 28)) == 3
    assert (settings.reports_dir / "Products_yesterday.csv").is_file()
    assert not list(settings.download_dir.iterdir())  # page exports archived
    assert len(list(settings.archive_dir.glob("*_yesterday-p*_Products.csv"))) == 2


def test_dry_run_writes_nothing(settings):
    table = make_products_table()
    summary = pipeline(settings, table, fake_export(settings, [[row("A")]], total=1), dry_run=True).run()
    assert summary.succeeded and summary.write is None
    assert table.count_for_date(date(2026, 10, 28)) == 0


def test_incomplete_export_changes_nothing(settings, tmp_path):
    table = make_products_table()
    kept = combine_exports([write(tmp_path / "kept.csv", export_text([row("Kept")]))]).frame
    table.replace_date(date(2026, 10, 28), table.to_records(kept, date(2026, 10, 28)))
    export = fake_export(settings, [[row("A")]], total=463)
    with pytest.raises(IncompleteExportError):
        pipeline(settings, table, export).run()
    assert table.count_for_date(date(2026, 10, 28)) == 1
    assert list(settings.failed_dir.iterdir())


def test_empty_page_keeps_existing_rows(settings):
    table = make_products_table()
    summary = pipeline(settings, table, lambda period: PagedExport(files=[], total=0), period="today").run()
    assert summary.succeeded and summary.write is None
    assert summary.period.report_date == date(2026, 10, 29)

