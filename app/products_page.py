"""Amazon Ads Campaign Manager -> Products page automation.

For one date preset (Today or Yesterday) this module:

1. opens ``cm/products?entityId=...`` in the persistent, signed-in session
   and makes sure the **Advertised products** tab is showing;
2. checks the required filter (``REQUIRED_FILTER_TEXT``, e.g.
   ``Clicks: greater than 0``) is applied;
3. opens **Date range**, clicks the preset, applies it, and checks that the
   button now shows the expected date;
4. sets **Results per page** to 200;
5. clicks **Export**, then the next-page arrow (>), then Export again ...
   until the "x-y of N results" label reaches N.

Each export is captured through the browser's download event, so a file is
only accepted once Chromium reports it complete.

Security boundaries are those of :mod:`app.auth`: the configured credentials
may be filled in, but CAPTCHA, OTP and MFA prompts stop the run for a person.

Every locator lives in ``config/selectors.json`` as an ordered candidate list.
When a run fails naming a key, put the working selector at the top of that list.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Download, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .auth import sign_in
from .browser import BrowserSession, safe_url
from .config import Settings
from .downloader import DownloadResult, target_filename, validate_download
from .exceptions import (
    AdFlowError,
    AuthenticationError,
    DateMismatchError,
    DownloadError,
    NavigationError,
    PageSetupError,
)
from .logging_config import get_logger
from .periods import ResolvedPeriod
from .products_csv import read_export
from .selectors import candidates, find_first, is_present

logger = get_logger("products-page")


# ---------------------------------------------------------------------------
# page text -- pure logic, unit-tested without a browser
# ---------------------------------------------------------------------------

#: The table footer: "1-200 of 463 results".
_RANGE = re.compile(r"(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+results?", re.IGNORECASE)
_NO_RESULTS = re.compile(r"\b0\s+results?\b", re.IGNORECASE)
#: "Results per page: 25".
_PER_PAGE = re.compile(r"results per page:?\s*(\d+)", re.IGNORECASE)
#: "Oct 28, 2026" on the Date range button (also "Sept 3, 2026").
_LABEL_DATE = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})\b")


@dataclass(frozen=True)
class PageRange:
    """The rows the table is showing: ``first``-``last`` of ``total``."""

    first: int
    last: int
    total: int

    @property
    def size(self) -> int:
        return self.last - self.first + 1 if self.total else 0

    @property
    def is_last(self) -> bool:
        return self.last >= self.total


def parse_range(text: str) -> PageRange | None:
    """``"1-200 of 463 results"`` -> PageRange(1, 200, 463); None if absent."""
    match = _RANGE.search(text)
    if match:
        first, last, total = (int(g.replace(",", "")) for g in match.groups())
        return PageRange(first, last, total)
    if _NO_RESULTS.search(text):
        return PageRange(0, 0, 0)
    return None


def parse_per_page(text: str) -> int | None:
    match = _PER_PAGE.search(text)
    return int(match.group(1)) if match else None


def dates_in_label(text: str) -> list[date]:
    found = []
    for month, day, year in _LABEL_DATE.findall(text):
        try:
            found.append(datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date())
        except ValueError:
            continue
    return found


def check_date_label(label: str, expected: date) -> bool:
    """Whether the Date range button confirms ``expected``.

    Returns False when the label holds no readable date (nothing to compare).

    Raises:
        DateMismatchError: The label shows any other date.
    """
    shown = dates_in_label(label)
    if not shown:
        return False
    if set(shown) != {expected}:
        raise DateMismatchError(
            f"The Date range shows {label!r}, expected {expected:%b} {expected.day}, {expected.year}. "
            "Nothing was exported."
        )
    return True


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


@dataclass
class PagedExport:
    """The files one run exported, in page order, and the page's result count."""

    files: list[DownloadResult] = field(default_factory=list)
    total: int = 0

    @property
    def paths(self) -> list[Path]:
        return [f.path for f in self.files]


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


class ProductsPage:
    """Operations against Campaign Manager's Products page."""

    #: Minimum seconds between two Export clicks.  Seen live 2026-10-30: an
    #: Export clicked 3 s after the previous one never produced a file, while
    #: one clicked 11 s after did.
    export_gap_s: float = 10.0

    def __init__(self, session: BrowserSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self._last_export_at: float | None = None

    @property
    def page(self) -> Page:
        return self.session.page

    # -- navigation and authentication --------------------------------------

    def open(self) -> None:
        """Load the Products page, signing in once if the session lapsed."""
        self._goto()
        if self.is_showing_sign_in():
            if not self.settings.can_auto_login:
                self.session.capture_failure("sign-in-required")
                raise AuthenticationError(
                    "Amazon session is missing or expired (sign-in page shown), and "
                    "automatic sign-in is not configured. Set AMAZON_EMAIL and "
                    "AMAZON_PASSWORD, or re-authenticate with: python run.py login"
                )
            sign_in(self.session, self.settings)
            self._goto()
        self.assert_authenticated()
        self.wait_until_loaded()
        self.show_advertised_products()
        self.check_required_filter()
        logger.info("Products page loaded")

    def _navigate(self, action: Callable[[], object], what: str) -> None:
        """Run a goto, following Amazon's own client-side redirect.

        The console rewrites its URL as it boots, which Playwright reports as
        the navigation being "interrupted by another navigation".  That is the
        page arriving, not failing: wait for the redirected page instead.
        """
        try:
            action()
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Timed out {what}: {exc}") from exc
        except PlaywrightError as exc:
            if "interrupted by another navigation" not in str(exc):
                raise NavigationError(f"Failed {what}: {exc}") from exc
            logger.info("Amazon redirected while %s; following it to %s", what, safe_url(self.page.url))
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=self.settings.browser_timeout_ms)
            except PlaywrightTimeoutError as wait_exc:
                raise NavigationError(f"Timed out after a redirect while {what}") from wait_exc

    def _goto(self) -> None:
        logger.info("Opening the Products page for %s", self.settings.amazon_entity_id)
        self._navigate(
            lambda: self.page.goto(self.settings.products_url, wait_until="domcontentloaded"),
            "loading the Products page",
        )

    def is_on_products_page(self) -> bool:
        return "/cm/products" in self.page.url.split("?", 1)[0]

    def is_showing_sign_in(self) -> bool:
        url = self.page.url.lower().split("?", 1)[0]
        if any(marker in url for marker in ("/ap/signin", "/ap/mfa", "/ap/cvf")):
            return True
        return is_present(self.page, "sign_in_marker", timeout_ms=1_500)

    def assert_authenticated(self) -> None:
        """Stop if Amazon shows sign-in or a CAPTCHA/OTP/MFA challenge."""
        if is_present(self.page, "security_challenge_marker", timeout_ms=1_500):
            self.session.capture_failure("security-challenge")
            raise AuthenticationError(
                "Amazon presented a security challenge (CAPTCHA/OTP/MFA). It is not "
                "answered automatically. Complete it once with 'python run.py login'."
            )
        if self.is_showing_sign_in():
            self.session.capture_failure("sign-in-required")
            raise AuthenticationError(
                "Amazon is still showing the sign-in page. Re-authenticate with: "
                "python run.py login"
            )

    def assert_no_page_error(self) -> None:
        """Stop on Amazon's own error panel ("Sorry, something went wrong").

        Transient: the retry reloads the page in a fresh browser.
        """
        if is_present(self.page, "page_error_marker", timeout_ms=500):
            self.session.capture_failure("amazon-page-error")
            raise NavigationError(
                "Amazon showed 'Sorry, something went wrong' instead of the Products page. "
                "If this persists, check in a normal browser that this login can open it "
                "and that AMAZON_ENTITY_ID is the right advertiser account."
            )

    def wait_until_loaded(self) -> None:
        deadline = time.monotonic() + self.settings.browser_timeout_ms / 1000
        marker = None
        while marker is None and time.monotonic() < deadline:
            self.assert_no_page_error()
            # Every candidate is checked on every pass (no per-candidate wait),
            # so a fallback is reached even when the first one never matches.
            for selector in candidates("products_loaded_marker"):
                try:
                    locator = self.page.locator(selector).first
                    if locator.is_visible():
                        marker = locator
                        break
                except Exception:
                    continue
            if marker is None:
                self.page.wait_for_timeout(500)
        if marker is None:
            self.session.capture_failure("products-page-not-loaded")
            raise NavigationError(
                f"The Products page did not render within {self.settings.browser_timeout_ms} ms "
                f"at {safe_url(self.page.url)} (config/selectors.json 'products_loaded_marker')."
            )
        self._wait_for_idle()

    def _wait_for_idle(self) -> None:
        budget = min(self.settings.browser_timeout_ms, 15_000)
        for selector in candidates("loading_indicator"):
            try:
                self.page.locator(selector).first.wait_for(state="hidden", timeout=budget)
            except Exception:
                continue
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(budget, 10_000))
        except PlaywrightTimeoutError:
            logger.debug("networkidle not reached; continuing")

    def _wait_until(self, condition: Callable[[], bool], what: str, label: str) -> None:
        """Poll ``condition`` until true, else capture the page and fail (transient)."""
        deadline = time.monotonic() + self.settings.browser_timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except AdFlowError:
                raise
            except Exception:  # the table is re-rendering; look again
                pass
            self.page.wait_for_timeout(500)
        self.session.capture_failure(label)
        raise NavigationError(f"Timed out waiting for {what} ({self.settings.browser_timeout_ms} ms)")

    def _require(self, name: str, *, timeout_ms: int = 10_000, values: dict[str, str] | None = None) -> Locator:
        control = find_first(self.page, name, timeout_ms=timeout_ms, values=values)
        if control is None:
            self.session.capture_failure(f"{name.replace('_', '-')}-missing")
            raise NavigationError(
                f"Could not find '{name}' on the Products page (config/selectors.json '{name}'). "
                "See the screenshot in data/screenshots/."
            )
        return control

    def _body_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5_000)
        except Exception:
            return ""

    # -- page setup -----------------------------------------------------------

    def show_advertised_products(self) -> None:
        """Select the Advertised products tab (not "All sold products")."""
        tab = find_first(self.page, "advertised_products_tab", timeout_ms=3_000)
        if tab is None:
            logger.debug("No Advertised products tab found; assuming the page opened on it")
            return
        if (tab.get_attribute("aria-selected") or "").lower() == "true":
            return
        tab.click()
        self._wait_for_idle()
        logger.info("Switched to the Advertised products tab")

    def check_required_filter(self) -> None:
        """REQUIRED_FILTER_TEXT (e.g. "Clicks: greater than 0") must be the only filter.

        Amazon remembers filters per login, so one added by hand in the UI
        stays.  Seen live 2026-10-30: an extra "Orders: equals 0" narrowed the
        export to zero-sales products.  Any other filter chip is removed here;
        without the required one the export would hold a different set of
        products than Power BI has always been loaded with.
        """
        expected = self.settings.required_filter_text
        if not expected:
            return
        self._remove_other_filters(_normalise(expected))
        if _normalise(expected) not in _normalise(self._body_text()):
            self.session.capture_failure("required-filter-missing")
            raise PageSetupError(
                f"The Products page does not show the filter {expected!r}. Add it once in a normal "
                "browser (Filter by -> ...); Amazon remembers it. Or change REQUIRED_FILTER_TEXT."
            )
        logger.info("Filter applied on the page: %s", expected)

    def _filter_tags(self) -> list[tuple[Locator, str]]:
        """Visible filter chips and their normalised text ("clicks: greater than 0 remove")."""
        for selector in candidates("filter_tag"):
            found = self.page.locator(selector)
            tags = []
            for i in range(found.count()):
                tag = found.nth(i)
                try:
                    if tag.is_visible():
                        tags.append((tag, _normalise(tag.inner_text(timeout=2_000))))
                except Exception:  # re-rendering; the next look sees it
                    continue
            if tags:
                return tags
        return []

    def _remove_other_filters(self, wanted: str) -> None:
        for _ in range(10):
            extra = [(tag, text) for tag, text in self._filter_tags() if wanted not in text]
            if not extra:
                return
            tag, text = extra[0]
            label = text.removesuffix(" remove")
            remove = next(
                (tag.locator(s).first for s in candidates("filter_tag_remove") if tag.locator(s).count()), None
            )
            if remove is None:
                self.session.capture_failure("filter-remove-missing")
                raise PageSetupError(
                    f"The page has the extra filter {label!r} and it could not be removed "
                    "(config/selectors.json 'filter_tag_remove'). Remove it once in a normal browser."
                )
            logger.warning("Removing the extra filter %r; only %r is wanted", label, wanted)
            remove.click()
            self._wait_until(
                lambda: all(t != text for _, t in self._filter_tags()),
                f"the filter {label!r} to go away",
                "filter-not-removed",
            )
            self._wait_for_idle()
        raise PageSetupError("Gave up removing extra filters after 10 attempts")

    def date_range_label(self) -> str:
        button = find_first(self.page, "date_range_button", timeout_ms=5_000)
        if button is None:
            return ""
        try:
            return " ".join(button.inner_text(timeout=2_000).split())
        except Exception:
            return ""

    def select_period(self, period: ResolvedPeriod) -> None:
        """Date range -> Today / Yesterday -> Apply, then confirm the date shown.

        Raises:
            DateMismatchError: The button shows another date afterwards.
        """
        self._require("date_range_button").click()
        preset = find_first(self.page, "date_preset", timeout_ms=10_000, values={"label": period.preset_label})
        if preset is None:
            self.page.keyboard.press("Escape")
            self.session.capture_failure("date-preset-missing")
            raise NavigationError(
                f"The Date range picker has no '{period.preset_label}' option "
                "(config/selectors.json 'date_preset')."
            )
        preset.click()
        apply = find_first(self.page, "date_apply_button", timeout_ms=3_000)
        if apply is not None and apply.is_enabled():
            apply.click()
        self.page.wait_for_timeout(1_000)
        self._wait_for_idle()

        label = self.date_range_label()
        if check_date_label(label, period.report_date):
            logger.info("Date range set to %s: %r", period.preset_label, label)
        else:
            logger.warning(
                "Date range set to %s, but the button text %r shows no date to confirm %s",
                period.preset_label,
                label,
                period.report_date,
            )

    def pagination(self) -> PageRange | None:
        """The table footer, e.g. "1-200 of 463 results"."""
        return parse_range(self._body_text())

    def set_results_per_page(self, size: int) -> None:
        button = self._require("results_per_page_button")
        try:
            current = parse_per_page(button.inner_text(timeout=2_000))
        except Exception:
            current = None
        if current == size:
            logger.info("Results per page is already %d", size)
            return
        button.click()
        option = find_first(self.page, "results_per_page_option", timeout_ms=5_000, values={"n": str(size)})
        if option is None:
            self.page.keyboard.press("Escape")
            self.session.capture_failure("results-per-page-option-missing")
            raise NavigationError(
                f"'Results per page' offers no {size} option (config/selectors.json 'results_per_page_option')."
            )
        option.click()

        def shows_page_size() -> bool:
            shown = self.pagination()
            return shown is not None and (shown.total == 0 or shown.size == min(size, shown.total - shown.first + 1))

        self._wait_until(shows_page_size, f"the table to show {size} rows per page", "results-per-page-not-applied")
        self._wait_for_idle()
        logger.info("Results per page set to %d (was %s)", size, current or "unknown")

    # -- export -----------------------------------------------------------------

    def export_all(self, period: ResolvedPeriod) -> PagedExport:
        """Export page 1, click >, export page 2, ... until the last page.

        Amazon's Export is cumulative: on page k it downloads rows 1 to the end
        of page k (seen live 2026-10-30: page 2 of 289 exported all 289).  So
        as soon as one export holds every result, it alone is the export and
        the partial ones before it are discarded.
        """
        shown = self.pagination()
        if shown is None:
            self.session.capture_failure("pagination-label-missing")
            raise NavigationError("Could not read the 'x-y of N results' label under the Products table")
        result = PagedExport(total=shown.total)
        if shown.total == 0:
            logger.warning("The Products page shows 0 results for %s", period.preset_label)
            return result
        if shown.first != 1:
            raise NavigationError(f"The table starts at row {shown.first}, expected 1 after changing the date")

        for number in range(1, self.settings.max_pages + 1):
            logger.info("Page %d: exporting rows %d-%d of %d", number, shown.first, shown.last, shown.total)
            exported = self.export_current_page(period, number)
            result.files.append(exported)
            rows = len(read_export(exported.path))
            if rows >= shown.total:
                for partial in result.files[:-1]:
                    partial.path.unlink(missing_ok=True)
                result.files = [exported]
                logger.info("Page %d export holds all %d rows; using it alone", number, rows)
                break
            if shown.is_last:
                break
            shown = self.next_page(shown)
        else:
            raise NavigationError(
                f"Stopped after MAX_PAGES={self.settings.max_pages} pages without reaching the last one"
            )
        logger.info("Exported %d page(s) for %s (%d results)", len(result.files), period.report_date, result.total)
        return result

    def next_page(self, current: PageRange) -> PageRange:
        button = self._require("next_page_button", timeout_ms=5_000)
        if not button.is_enabled() or (button.get_attribute("aria-disabled") or "").lower() == "true":
            self.session.capture_failure("next-page-disabled")
            raise NavigationError(f"The next-page button is disabled at rows {current.first}-{current.last}")
        button.click()
        expected_first = current.last + 1

        def moved() -> bool:
            shown = self.pagination()
            return shown is not None and shown.first == expected_first

        self._wait_until(moved, f"the table to show rows from {expected_first}", "next-page-not-loaded")
        self._wait_for_idle()
        shown = self.pagination()
        assert shown is not None
        return shown

    def export_current_page(self, period: ResolvedPeriod, number: int) -> DownloadResult:
        """Click Export and save the file it produces.

        Export may download at once, open a small menu (e.g. CSV), or open a
        dialog with its own button; each is tried in turn while waiting.
        """
        control = self._require("export_button")
        if self._last_export_at is not None:
            pause = self.export_gap_s - (time.monotonic() - self._last_export_at)
            if pause > 0:
                logger.info("Waiting %.0f s before the next Export", pause)
                self.page.wait_for_timeout(pause * 1000)
        self._wait_until(
            lambda: control.is_enabled() and (control.get_attribute("aria-disabled") or "").lower() != "true",
            "the Export button to be enabled",
            "export-disabled",
        )
        captured: list[Download] = []
        popups: list[Page] = []

        def on_download(download: Download) -> None:
            captured.append(download)

        def on_page(page: Page) -> None:
            popups.append(page)
            page.on("download", on_download)

        context = self.session.context
        self.page.on("download", on_download)
        context.on("page", on_page)
        try:
            control.click()
            started = time.monotonic()
            deadline = started + self.settings.download_timeout_ms / 1000
            menu_tried = confirm_tried = reclicked = False
            popup_seen_at: float | None = None
            while not captured and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
                elapsed = time.monotonic() - started
                if not menu_tried and elapsed > 2:
                    menu_tried = True
                    item = find_first(self.page, "export_menu_item", timeout_ms=1_500)
                    if item is not None and not captured:
                        logger.info("Export opened a menu; choosing %r", " ".join(item.inner_text().split()))
                        item.click()
                if not confirm_tried and elapsed > 5:
                    confirm_tried = True
                    confirm = find_first(self.page, "export_confirm_button", timeout_ms=1_500)
                    if confirm is not None and not captured:
                        logger.info("Export opened a dialog; confirming it")
                        confirm.click()
                if not reclicked and not popups and elapsed > 30:
                    reclicked = True
                    logger.warning("No file 30 s after Export on page %d; clicking Export again", number)
                    control.click()
                if popups and popup_seen_at is None:
                    popup_seen_at = time.monotonic()
                if popup_seen_at is not None and time.monotonic() - popup_seen_at > 15:
                    break  # a tab opened and nothing downloaded from it
        finally:
            self.page.remove_listener("download", on_download)
            context.remove_listener("page", on_page)

        self._last_export_at = time.monotonic()
        label = f"{period.period}-p{number:02d}"
        if captured:
            return self._save_download(captured[0], label)
        if popups:
            return self._fetch_popup(popups[0], label)
        self.session.capture_failure("export-timeout")
        raise DownloadError(
            f"Clicked Export on page {number}, but no file arrived within {self.settings.download_timeout_ms} ms "
            "(config/selectors.json 'export_button' / 'export_menu_item')."
        )

    def _save_download(self, download: Download, label: str) -> DownloadResult:
        failure = download.failure()
        if failure:
            self.session.capture_failure("download-failed")
            raise DownloadError(f"Download failed: {failure}")
        suggested = download.suggested_filename or "Products.csv"
        target = self.settings.download_dir / target_filename(suggested, label)
        try:
            download.save_as(str(target))
        except Exception as exc:
            raise DownloadError(f"Could not save download to {target}: {exc}") from exc
        logger.info("Export captured: %s (suggested name %s)", target, suggested)
        size = validate_download(target)
        return DownloadResult(path=target, size_bytes=size, suggested_filename=suggested)

    def _fetch_popup(self, popup: Page, label: str) -> DownloadResult:
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            url = popup.url
            response = self.session.context.request.get(url, timeout=self.settings.download_timeout_ms)
        except Exception as exc:
            raise DownloadError(f"Could not fetch the export from the opened tab: {exc}") from exc
        finally:
            try:
                popup.close()
            except Exception:
                pass
        if not response.ok:
            raise DownloadError(f"Export link returned HTTP {response.status}")
        name = safe_url(url).rsplit("/", 1)[-1].split("?", 1)[0] or "Products.csv"
        target = self.settings.download_dir / target_filename(name, label)
        target.write_bytes(response.body())
        logger.info("Export fetched from opened tab: %s", target)
        size = validate_download(target)
        return DownloadResult(path=target, size_bytes=size, suggested_filename=name)
