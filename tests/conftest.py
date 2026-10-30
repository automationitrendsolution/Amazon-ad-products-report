"""Shared fixtures.  Nothing here touches Amazon or a real MySQL server.

Database tests run against in-memory SQLite with a ``Products`` table shaped
like production (a Date column plus the twelve export columns), so the
transaction and its rollback are exercised for real.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, create_engine
from sqlalchemy.pool import StaticPool

from app.config import REQUIRED_COLUMNS, Settings, load_settings
from app.database import ProductsTable

BASE_ENV = {
    "DB_HOST": "localhost",
    "DB_NAME": "powerbidb",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
    "AMAZON_ENTITY_ID": "ENTITYTEST123",
    "AMAZON_ACCOUNT_TIMEZONE": "America/Los_Angeles",
    "REQUIRED_FILTER_TEXT": "Clicks: greater than 0",
}

HEADER = ",".join(f'"{c}"' if "," in c else c for c in REQUIRED_COLUMNS)


def row(product: str, sales: str = "$1,104.98", *, impressions: str = "36,849", clicks: str = "410",
        conversion: str = "11.24%", eligibility: str = "Eligible") -> str:
    """One Products export data row, formatted as the page shows it."""
    return (
        f'"{product}",Delivering,Product,{eligibility},"{sales}",{conversion},"{impressions}",{clicks},'
        f'"$355.18",46,12,"$287.50"'
    )


def export_text(rows: list[str], *, preamble: str = "") -> str:
    return preamble + "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ignore the developer's .env and exported variables."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *_a, **_k: False)
    for key in list(os.environ):
        if key.startswith(("AMAZON_", "DB_", "REPORT", "REQUIRED_", "RESULTS_", "MAX_", "PRODUCTS_",
                           "DOWNLOAD_", "ARCHIVE_", "FAILED_", "LOG_", "SCREENSHOT_", "SESSION_", "LOCK_",
                           "HEADLESS", "BROWSER_", "RETRY_", "FILE_RETENTION")):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, folder in (
        ("REPORTS_DIR", "reports"),
        ("DOWNLOAD_DIR", "downloads"),
        ("ARCHIVE_DIR", "archive"),
        ("FAILED_DIR", "failed"),
        ("SCREENSHOT_DIR", "screenshots"),
        ("SESSION_DIR", "session"),
        ("LOG_DIR", "logs"),
    ):
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "run.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    loaded = load_settings()
    loaded.ensure_directories()
    return loaded


def make_products_table(
    *,
    date_type=Text,
    numeric: bool = True,
    extra: list[Column] | None = None,
    omit: set[str] = frozenset(),
) -> ProductsTable:
    """An in-memory ``Products`` table: numbers as REAL/INTEGER, or everything as text."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata = MetaData()
    integer_columns = {"Impressions", "Clicks", "Orders", "NTB orders"}
    number_columns = {"Sales(USD)", "Conversion rate", "Spend(USD)", "NTB sales(USD)"}
    columns = [Column("id", Integer, primary_key=True, autoincrement=True), Column("Date", date_type, nullable=False)]
    for name in REQUIRED_COLUMNS:
        if name in omit:
            continue
        if numeric and name in integer_columns:
            columns.append(Column(name, Integer))
        elif numeric and name in number_columns:
            columns.append(Column(name, Float))
        else:
            columns.append(Column(name, String(255)))
    Table("Products", metadata, *columns, *(extra or []))
    metadata.create_all(engine)
    table = ProductsTable(engine)
    table.reflect()
    table.validate_schema()
    return table


@pytest.fixture
def products_table() -> ProductsTable:
    return make_products_table()


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
