"""The page-reading logic of app.products_page, without a browser."""

from __future__ import annotations

from datetime import date

import pytest

from app.exceptions import DateMismatchError
from app.products_page import PageRange, check_date_label, dates_in_label, parse_per_page, parse_range


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Results per page: 25  1-25 of 451 results", PageRange(1, 25, 451)),
        ("1-200 of 463 results", PageRange(1, 200, 463)),
        ("201 – 400 of 1,463 results", PageRange(201, 400, 1463)),
        ("401-463 of 463 results", PageRange(401, 463, 463)),
        ("1-1 of 1 result", PageRange(1, 1, 1)),
        ("0 results", PageRange(0, 0, 0)),
        ("no footer here", None),
    ],
)
def test_parse_range(text, expected):
    assert parse_range(text) == expected


def test_range_properties():
    assert PageRange(1, 200, 463).size == 200
    assert not PageRange(1, 200, 463).is_last
    assert PageRange(401, 463, 463).is_last
    assert PageRange(0, 0, 0).is_last


def test_parse_per_page():
    assert parse_per_page("Results per page: 25") == 25
    assert parse_per_page("Results per page:200") == 200
    assert parse_per_page("Export") is None


def test_dates_in_label():
    assert dates_in_label("Date range: Oct 28, 2026") == [date(2026, 10, 28)]
    assert dates_in_label("Sept 3, 2026 - Sept 4, 2026") == [date(2026, 9, 3), date(2026, 9, 4)]
    assert dates_in_label("Date range: Yesterday") == []


def test_check_date_label_confirms_the_expected_day():
    assert check_date_label("Date range: Oct 28, 2026", date(2026, 10, 28)) is True
    assert check_date_label("Oct 28, 2026 - Oct 28, 2026", date(2026, 10, 28)) is True
    assert check_date_label("Date range: Yesterday", date(2026, 10, 28)) is False


def test_check_date_label_refuses_another_day():
    with pytest.raises(DateMismatchError, match="Oct 28, 2026"):
        check_date_label("Date range: Oct 27, 2026", date(2026, 10, 28))
    with pytest.raises(DateMismatchError):
        check_date_label("Oct 21, 2026 - Oct 28, 2026", date(2026, 10, 28))
