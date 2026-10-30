"""Environment-driven configuration.

Every secret arrives through the process environment (populated from a
mode-600 ``.env`` file, or exported by the scheduler).  Validation is strict and
fails fast: a run with a bad timezone or entity ID never opens a browser.

The destination is fixed: database ``powerbidb``, table ``Products``.  Neither
is configurable to anything else, so no setting can point this workflow at
another table.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: The only database and table this project is allowed to write to.
TARGET_DATABASE = "powerbidb"
PRODUCTS_TABLE = "Products"
#: The table's date column; one Amazon date = one set of rows.
DATE_COLUMN = "Date"

#: Columns every page export must contain, as Amazon names them.  The same
#: twelve the manual products_secondary_upload.py script required.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "Products",
    "Status",
    "Type",
    "Sponsored Products eligibility",
    "Sales(USD)",
    "Conversion rate",
    "Impressions",
    "Clicks",
    "Spend(USD)",
    "Orders",
    "NTB orders",
    "NTB sales(USD)",
)

UTC = ZoneInfo("UTC")

#: Campaign Manager -> Products (Advertised products tab).
DEFAULT_PRODUCTS_URL_TEMPLATE = "https://advertising.amazon.com/cm/products?entityId={entity_id}"
ENTITY_ID_PATTERN = re.compile(r"^ENTITY[A-Z0-9]+$")

#: The date presets this project selects on the page.
PERIODS: tuple[str, ...] = ("today", "yesterday")
#: Page sizes Amazon offers in "Results per page".
RESULTS_PER_PAGE_CHOICES: tuple[int, ...] = (25, 50, 100, 200)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false), got {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _resolve_path(raw: str | None, default: Path) -> Path:
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _get_path(name: str, default: Path) -> Path:
    return _resolve_path(_get(name), default)


def _get_zone(name: str, default: str | None) -> ZoneInfo:
    raw = _get(name, default)
    if not raw:
        raise ConfigurationError(
            f"Required environment variable {name} is not set (an IANA timezone, "
            "e.g. Asia/Kolkata)"
        )
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{name}={raw!r} is not a valid IANA timezone. "
            "Install the 'tzdata' package if the system zone database is missing."
        ) from exc


def _validate_strftime(name: str, value: str) -> None:
    try:
        probe = date(2026, 1, 31).strftime(value)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"{name}={value!r} is not a valid strftime format") from exc
    if probe == value:
        raise ConfigurationError(f"{name}={value!r} contains no date directives")


_DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)


def _browser_args() -> tuple[str, ...]:
    args = list(_DEFAULT_BROWSER_ARGS)
    extra = _get("BROWSER_ARGS")
    if extra:
        args.extend(part.strip() for part in extra.split(",") if part.strip())
    if _get_bool("BROWSER_NO_SANDBOX", False):
        args.append("--no-sandbox")
    return tuple(dict.fromkeys(args))


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 30
    ssl_ca: Path | None = None
    ssl_verify_cert: bool = False
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        """Driver kwargs for mysql-connector-python."""
        args: dict[str, object] = {"connection_timeout": self.connect_timeout_seconds}
        if self.ssl_disabled:
            args["ssl_disabled"] = True
            return args
        if self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        """SQLAlchemy URL.  Credentials are percent-encoded, never logged."""
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def safe_url(self) -> str:
        """Same URL with the password redacted -- this is the loggable form."""
        return f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    database: DatabaseConfig

    #: Products page URL with an ``{entity_id}`` placeholder.
    products_url_template: str
    #: Advertiser account (``entityId=`` in the console URL).
    amazon_entity_id: str
    #: Text the page must show before exporting, e.g. "Clicks: greater than 0".
    required_filter_text: str | None
    #: "Results per page" chosen before exporting.
    results_per_page: int
    #: Safety stop for the page loop.
    max_pages: int

    amazon_email: str | None
    amazon_password: str | None = field(repr=False)
    auto_login: bool

    #: Operator's timezone: only used to show local time in the logs.
    report_timezone: ZoneInfo
    #: The Amazon advertiser account's timezone: the calendar the Today /
    #: Yesterday presets follow, and so the date each load is stored under.
    amazon_account_timezone: ZoneInfo
    #: How Date is written when the destination column is text.
    db_date_format: str

    #: Where the latest merged export of each period is published.
    reports_dir: Path
    download_dir: Path
    archive_dir: Path
    failed_dir: Path
    log_dir: Path
    screenshot_dir: Path
    session_dir: Path
    lock_file: Path

    headless: bool
    browser_timeout_ms: int
    download_timeout_ms: int
    slow_mo_ms: int
    user_agent: str | None
    browser_args: tuple[str, ...]
    viewport_width: int
    viewport_height: int

    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float

    retention_days: int
    log_level: str
    log_retention_days: int

    @property
    def products_url(self) -> str:
        return self.products_url_template.format(entity_id=self.amazon_entity_id)

    @property
    def has_credentials(self) -> bool:
        return bool(self.amazon_email and self.amazon_password)

    @property
    def can_auto_login(self) -> bool:
        return self.auto_login and self.has_credentials

    def ensure_directories(self) -> None:
        """Create every directory the workflow writes to.  Idempotent."""
        for path in (
            self.download_dir,
            self.reports_dir,
            self.archive_dir,
            self.failed_dir,
            self.log_dir,
            self.screenshot_dir,
            self.session_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "database": self.database.safe_url,
            "table": f"{TARGET_DATABASE}.{PRODUCTS_TABLE}",
            "amazon_tz": str(self.amazon_account_timezone),
            "entity_id": self.amazon_entity_id,
            "required_filter": self.required_filter_text or "(not checked)",
            "per_page": self.results_per_page,
            "db_date_format": self.db_date_format,
            "credentials": "configured" if self.has_credentials else "not configured",
            "auto_login": self.can_auto_login,
            "headless": self.headless,
            "max_retries": self.max_retries,
        }


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Args:
        env_file: Optional explicit ``.env`` path.  Defaults to ``.env`` in the
            project root when it exists.
        override: Whether ``.env`` values replace already-exported variables.

    Raises:
        ConfigurationError: If a required value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    db_name = _get("DB_NAME", TARGET_DATABASE) or TARGET_DATABASE
    if db_name != TARGET_DATABASE:
        raise ConfigurationError(
            f"DB_NAME is {db_name!r}, but this project only writes to the "
            f"{TARGET_DATABASE} database. Refusing to run against another database."
        )

    ssl_ca_raw = _get("DB_SSL_CA")
    ssl_ca = None
    if ssl_ca_raw:
        ssl_ca = _resolve_path(ssl_ca_raw, PROJECT_ROOT)
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    database = DatabaseConfig(
        host=_require("DB_HOST"),
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=db_name,
        user=_require("DB_USER"),
        password=_require("DB_PASSWORD"),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )

    entity_id = _require("AMAZON_ENTITY_ID")
    if not ENTITY_ID_PATTERN.match(entity_id):
        raise ConfigurationError(
            f"AMAZON_ENTITY_ID={entity_id!r} is not an entity ID; copy the entityId=ENTITY... "
            "value from the Products page URL"
        )
    template = _get("PRODUCTS_URL_TEMPLATE", DEFAULT_PRODUCTS_URL_TEMPLATE)
    assert template is not None
    parts = urlsplit(template)
    if parts.scheme != "https" or not parts.netloc.startswith("advertising.amazon."):
        raise ConfigurationError(
            f"PRODUCTS_URL_TEMPLATE must be an https://advertising.amazon.* URL, got {template!r}"
        )
    if "{entity_id}" not in template:
        raise ConfigurationError("PRODUCTS_URL_TEMPLATE must contain {entity_id}")

    results_per_page = _get_int("RESULTS_PER_PAGE", 200, minimum=1)
    if results_per_page not in RESULTS_PER_PAGE_CHOICES:
        raise ConfigurationError(
            f"RESULTS_PER_PAGE must be one of {', '.join(map(str, RESULTS_PER_PAGE_CHOICES))}; "
            f"got {results_per_page}"
        )

    db_date_format = _get("DB_DATE_FORMAT", "%Y-%m-%d") or "%Y-%m-%d"
    _validate_strftime("DB_DATE_FORMAT", db_date_format)

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    settings = Settings(
        database=database,
        products_url_template=template,
        amazon_entity_id=entity_id,
        required_filter_text=_get("REQUIRED_FILTER_TEXT"),
        results_per_page=results_per_page,
        max_pages=_get_int("MAX_PAGES", 50, minimum=1),
        amazon_email=_get("AMAZON_EMAIL"),
        amazon_password=_get("AMAZON_PASSWORD"),
        auto_login=_get_bool("AMAZON_AUTO_LOGIN", True),
        report_timezone=_get_zone("REPORT_TIMEZONE", "Asia/Kolkata"),
        amazon_account_timezone=_get_zone("AMAZON_ACCOUNT_TIMEZONE", "America/Los_Angeles"),
        db_date_format=db_date_format,
        reports_dir=_get_path("REPORTS_DIR", data_dir / "reports"),
        download_dir=_get_path("DOWNLOAD_DIR", data_dir / "downloads"),
        archive_dir=_get_path("ARCHIVE_DIR", data_dir / "archive"),
        failed_dir=_get_path("FAILED_DIR", data_dir / "failed"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        screenshot_dir=_get_path("SCREENSHOT_DIR", data_dir / "screenshots"),
        session_dir=_get_path("SESSION_DIR", data_dir / "session"),
        lock_file=_get_path("LOCK_FILE", data_dir / "products_report.lock"),
        headless=_get_bool("HEADLESS", True),
        browser_timeout_ms=_get_int("BROWSER_TIMEOUT_MS", 60_000, minimum=1_000),
        download_timeout_ms=_get_int("DOWNLOAD_TIMEOUT_MS", 120_000, minimum=1_000),
        slow_mo_ms=_get_int("SLOW_MO_MS", 0, minimum=0),
        user_agent=_get("BROWSER_USER_AGENT"),
        browser_args=_browser_args(),
        viewport_width=_get_int("VIEWPORT_WIDTH", 1920, minimum=320),
        viewport_height=_get_int("VIEWPORT_HEIGHT", 1080, minimum=320),
        max_retries=_get_int("MAX_RETRIES", 2, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 10.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        retention_days=_get_int("FILE_RETENTION_DAYS", 7, minimum=1),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 14, minimum=1),
    )

    if bool(settings.amazon_email) != bool(settings.amazon_password):
        missing = "AMAZON_PASSWORD" if settings.amazon_email else "AMAZON_EMAIL"
        raise ConfigurationError(
            f"{missing} is not set. Automatic sign-in needs both AMAZON_EMAIL and "
            "AMAZON_PASSWORD, or neither (session-only mode)."
        )
    return settings
