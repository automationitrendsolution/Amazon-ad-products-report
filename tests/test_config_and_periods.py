from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import load_settings
from app.exceptions import ConfigurationError
from app.periods import resolve_period

IST = ZoneInfo("Asia/Kolkata")


def test_products_url_carries_the_entity(settings):
    assert settings.products_url == "https://advertising.amazon.com/cm/products?entityId=ENTITYTEST123"
    assert settings.results_per_page == 200
    assert settings.required_filter_text == "Clicks: greater than 0"


def test_entity_id_is_required(env, monkeypatch):
    monkeypatch.delenv("AMAZON_ENTITY_ID")
    with pytest.raises(ConfigurationError, match="AMAZON_ENTITY_ID"):
        load_settings()


def test_refuses_another_database(env, monkeypatch):
    monkeypatch.setenv("DB_NAME", "otherdb")
    with pytest.raises(ConfigurationError, match="powerbidb"):
        load_settings()


def test_results_per_page_must_be_an_amazon_choice(env, monkeypatch):
    monkeypatch.setenv("RESULTS_PER_PAGE", "150")
    with pytest.raises(ConfigurationError, match="RESULTS_PER_PAGE"):
        load_settings()


def test_password_without_email_is_refused(env, monkeypatch):
    monkeypatch.setenv("AMAZON_PASSWORD", "x")
    with pytest.raises(ConfigurationError, match="AMAZON_EMAIL"):
        load_settings()


@pytest.mark.parametrize(
    ("ist", "period", "expected"),
    [
        # 14:00 IST Oct 29 = 01:30 Oct 29 in Los Angeles (summer): Yesterday is
        # Oct 28, the US day that ended at 12:30 IST.
        (datetime(2026, 10, 29, 14, 0, tzinfo=IST), "yesterday", date(2026, 10, 28)),
        (datetime(2026, 10, 29, 14, 0, tzinfo=IST), "today", date(2026, 10, 29)),
        # 10:00 IST is still the US evening of the previous IST day.
        (datetime(2026, 10, 29, 10, 0, tzinfo=IST), "today", date(2026, 10, 28)),
        # 15:00 IST: a new US day has started.
        (datetime(2026, 10, 29, 15, 0, tzinfo=IST), "today", date(2026, 10, 29)),
        # US winter time: 14:00 IST = 00:30 in Los Angeles, still after midnight.
        (datetime(2026, 12, 10, 14, 0, tzinfo=IST), "yesterday", date(2026, 12, 9)),
    ],
)
def test_period_dates_follow_the_amazon_calendar(settings, ist, period, expected):
    resolved = resolve_period(settings, period, reference=ist)
    assert resolved.report_date == expected
    assert resolved.preset_label == period.capitalize()


def test_unknown_period(settings):
    with pytest.raises(ConfigurationError):
        resolve_period(settings, "last-week")
