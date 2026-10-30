"""Pipeline orchestration and execution modes.

    check powerbidb.Products                       (before any browser work)
      -> Products page, dated in the Amazon account calendar:
           Date range -> Today | Yesterday -> Apply   (date on the button checked)
           Results per page -> 200
           Export, >, Export, >, ... until "x-y of N results" reaches N
      -> merge the page exports; check columns, repeats, and that rows == N
      -> one transaction: DELETE that date + INSERT -> COMMIT
      -> publish data/reports/Products_<period>.csv, archive the page exports

Nothing is deleted from MySQL unless a complete, validated export for that
date is ready to insert in the same transaction.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .auth import sign_in_interactive
from .browser import BrowserSession, safe_url
from .config import PRODUCTS_TABLE, TARGET_DATABASE, Settings
from .database import ProductsTable, WriteResult, build_products_table
from .exceptions import (
    AdFlowError,
    AuthenticationError,
    DatabaseError,
    NavigationError,
)
from .logging_config import get_logger
from .maintenance import cleanup_old_files
from .periods import ResolvedPeriod, resolve_period
from .products_csv import ProductsExport, combine_exports, write_merged
from .products_page import PagedExport, ProductsPage
from .retry import retry_call
from .runlock import run_lock
from .timeutils import now_in, now_utc

logger = get_logger("pipeline")

T = TypeVar("T")


class RunMode(str, Enum):
    LOAD_PRODUCTS = "load-products"
    LOGIN = "login"


@dataclass
class RunOptions:
    mode: RunMode = RunMode.LOAD_PRODUCTS
    #: "today" or "yesterday": the Date range preset to export.
    period: str = "today"
    dry_run: bool = False
    use_lock: bool = True

    @property
    def writes_database(self) -> bool:
        return not self.dry_run and self.mode is RunMode.LOAD_PRODUCTS

    @property
    def uses_database(self) -> bool:
        return self.mode is RunMode.LOAD_PRODUCTS

    @property
    def label(self) -> str:
        label = self.period if self.mode is RunMode.LOAD_PRODUCTS else self.mode.value
        return f"{label} (dry-run)" if self.dry_run else label


@dataclass
class RunSummary:
    mode: str
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    period: ResolvedPeriod | None = None
    pages: int = 0
    page_total: int | None = None
    rows: int = 0
    saved_to: Path | None = None
    write: WriteResult | None = None
    error: str | None = None
    succeeded: bool = False

    def log(self) -> None:
        end = self.finished_at or now_utc()
        logger.info("=" * 72)
        logger.info("Run summary (%s)", self.mode)
        logger.info("  started / finished : %s / %s (UTC)", self.started_at, end)
        logger.info("  duration           : %.1fs", (end - self.started_at).total_seconds())
        if self.period:
            logger.info("  date range         : %s = %s", self.period.preset_label, self.period.report_date)
        if self.pages:
            shown = f" (page shows {self.page_total})" if self.page_total is not None else ""
            logger.info("  exported           : %d page(s), %d product row(s)%s", self.pages, self.rows, shown)
        if self.saved_to:
            logger.info("  saved              : %s", self.saved_to)
        if self.write:
            logger.info(
                "  %-19s: %d deleted, %d inserted for %s",
                self.write.table, self.write.deleted, self.write.inserted, self.write.report_date,
            )
        else:
            logger.info("  database           : not modified")
        logger.info("  status             : %s", "SUCCESS" if self.succeeded else "FAILED")
        if self.error:
            logger.info("  error              : %s", self.error)
        logger.info("=" * 72)


class ProductsReportPipeline:
    """One execution end to end."""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        database_factory: Callable[[], ProductsTable] | None = None,
        export_fn: Callable[[ResolvedPeriod], PagedExport] | None = None,
        reference_time: datetime | None = None,
    ) -> None:
        """
        Args:
            database_factory: Overrides how the MySQL connection is built (tests).
            export_fn: Overrides the browser export stage (tests).
            reference_time: Pretend "now" for the Today / Yesterday dates (tests).
        """
        self.settings = settings
        self.options = options
        self.summary = RunSummary(mode=options.label)
        self._reference_time = reference_time
        self._owns_database = database_factory is None
        self._database_factory = database_factory or (
            lambda: build_products_table(settings.database, date_format=settings.db_date_format)
        )
        self._export_fn = export_fn or self._browser_export

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        self.settings.ensure_directories()
        self._log_start()
        try:
            with run_lock(self.settings.lock_file, enabled=self.options.use_lock):
                if self.options.mode is RunMode.LOGIN:
                    self._run_login()
                else:
                    self._run_export()
            self.summary.succeeded = True
        except AdFlowError as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.error("Run failed: %s", self.summary.error)
            raise
        except Exception as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Unexpected failure")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        logger.info("=" * 72)
        logger.info("Products report starting | mode=%s", self.options.label)
        logger.info("  UTC            : %s", now_utc().isoformat(timespec="seconds"))
        for zone in (self.settings.report_timezone, self.settings.amazon_account_timezone):
            logger.info("  %-14s : %s", str(zone), now_in(zone).isoformat(timespec="seconds"))
        described = self.settings.describe()
        if not self.options.uses_database:
            described["table"] = "(none: no MySQL in this mode)"
            described.pop("db_date_format", None)
        for key, value in described.items():
            logger.info("  %-14s : %s", key, value)
        logger.info("=" * 72)

    # -- modes -------------------------------------------------------------

    def _run_export(self) -> None:
        """Export the selected date from the Products page, validate, load, publish.

        The date is replaced in powerbidb.Products (nothing is written with
        ``--dry-run``); the merged CSV is published only after the COMMIT.
        """
        period = resolve_period(self.settings, self.options.period, reference=self._reference_time)
        self.summary.period = period
        logger.info(
            "Load-products: %s (%s) -> %s.%s",
            period.preset_label,
            period.report_date,
            TARGET_DATABASE,
            PRODUCTS_TABLE,
        )
        # Before any browser work: an unreachable server or a mismatched table
        # would otherwise only show up after every page has been exported.
        database = self._open_database()
        try:
            paged, export = self._retry("Products export", lambda: self._export_and_check(period))
            self.summary.pages = len(paged.files)
            self.summary.page_total = paged.total
            self.summary.rows = export.row_count if export else 0
            if export is None or export.row_count == 0:
                # Never a delete-only write: the date's existing rows are kept.
                logger.warning("The Products page has no rows for %s yet; existing rows kept", period.report_date)
                return
            self._load(export, period.report_date, database)
            stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
            merged = write_merged(export, self.settings.archive_dir / f"{stamp}_Products_{period.period}.csv")
            self.summary.saved_to = _publish(merged, self.settings.reports_dir / f"Products_{period.period}.csv")
            for page_file in paged.paths:
                self._move(page_file, self.settings.archive_dir)
            logger.info("Saved %s export: %s (%d row(s))", period.period, self.summary.saved_to, export.row_count)
        finally:
            if database is not None and self._owns_database:
                database.dispose()
            try:
                cleanup_old_files(self.settings)
            except Exception:
                logger.warning("Housekeeping failed", exc_info=True)

    def _export_and_check(self, period: ResolvedPeriod) -> tuple[PagedExport, ProductsExport | None]:
        """One attempt: export every page, then merge and check the files.

        Inside the retry, so an export that does not add up to the page's
        result count is tried again in a fresh browser.
        """
        paged = self._export_fn(period)
        if not paged.files:
            return paged, None
        try:
            return paged, combine_exports(paged.paths, expected_total=paged.total)
        except AdFlowError:
            for page_file in paged.paths:
                self._move(page_file, self.settings.failed_dir)
            raise

    def _load(self, export: ProductsExport, day: date, database: ProductsTable | None) -> None:
        if database is None or not self.options.writes_database:
            logger.info("DRY RUN: %d row(s) prepared for %s; nothing written", export.row_count, day)
            if database is not None:
                database.to_records(export.frame, day)  # still proves every value converts
            return
        records = database.to_records(export.frame, day)
        self.summary.write = self._retry(f"Database load for {day}", lambda: database.replace_date(day, records))

    # -- stages ------------------------------------------------------------

    def _open_database(self) -> ProductsTable | None:
        try:
            return self._retry("Database connection", self._database_factory)
        except DatabaseError:
            if self.options.writes_database:
                raise
            logger.warning("DRY RUN: MySQL unavailable; continuing without schema validation")
            return None

    def _browser_export(self, period: ResolvedPeriod) -> PagedExport:
        """One attempt: open, choose the date, 200 per page, export every page."""
        with BrowserSession(self.settings) as session:
            products = ProductsPage(session, self.settings)
            products.open()
            products.select_period(period)
            products.set_results_per_page(self.settings.results_per_page)
            return products.export_all(period)

    def _retry(self, description: str, operation: Callable[[], T]) -> T:
        return retry_call(
            operation,
            description=description,
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )

    def _move(self, path: Path, directory: Path) -> Path | None:
        if not path.exists():
            return None
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / path.name
        try:
            shutil.move(str(path), str(destination))
            logger.debug("Moved %s to %s", path.name, directory)
            return destination
        except OSError as exc:
            logger.warning("Could not move %s to %s: %s", path, directory, exc)
            return None

    # -- interactive login -------------------------------------------------

    def _run_login(self) -> None:
        """Establish the Amazon session once, with a person at the keyboard.

        Headless, exactly like the scheduled runs, so Amazon sees the same
        browser later.  The email and password come from .env; Amazon's
        two-step code (OTP) is typed at the terminal prompt, and "don't ask
        again on this browser" is ticked, so cron never needs a code.
        """
        with BrowserSession(self.settings, headless=True) as session:
            products = ProductsPage(session, self.settings)
            products._goto()
            if products.is_showing_sign_in():
                sign_in_interactive(session, self.settings)
                # Amazon normally lands back on the page itself after MFA;
                # only navigate again if it did not.
                if not products.is_on_products_page():
                    products._goto()
            else:
                print("\nAlready signed in -- the saved session is still valid.")
            print("\nChecking the session...")
            products.assert_authenticated()
            if not products.is_on_products_page():
                raise AuthenticationError(
                    f"Signed in, but the browser is not on the Products page "
                    f"({safe_url(session.page.url)}). Run 'python run.py login' again."
                )
            print("  OK: signed in and on the Products page")
            try:
                products.wait_until_loaded()
                print("  OK: Products table recognised")
            except NavigationError:
                # The session is fine; the page selector is not.
                print("  NOTE: signed in, but 'products_loaded_marker' did not match (config/selectors.json).")
        logger.info("Session saved to %s", self.settings.session_dir)
        print(f"\nSession stored in {self.settings.session_dir}. Next: python run.py yesterday --dry-run\n")


def _publish(source: Path, target: Path) -> Path:
    """Copy ``source`` over ``target`` atomically (readers never see half a file)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)
    return target


def handle_failure(pipeline: ProductsReportPipeline, error: BaseException) -> None:
    """Operator-facing guidance for a failed run."""
    table = f"{TARGET_DATABASE}.{PRODUCTS_TABLE}"
    if isinstance(error, AuthenticationError):
        logger.error("Authentication needs a person. Run 'python run.py login' to refresh the session.")
    elif isinstance(error, DatabaseError):
        logger.error("Nothing was committed; existing rows in %s are intact.", table)
    else:
        logger.error("Nothing was written to %s; see the summary above.", table)
