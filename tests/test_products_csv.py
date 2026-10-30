from __future__ import annotations

import pytest

from app.config import REQUIRED_COLUMNS
from app.exceptions import CSVValidationError, IncompleteExportError
from app.products_csv import combine_exports, parse_number, read_export

from .conftest import HEADER, export_text, row, write


def test_reads_the_required_columns_only(tmp_path):
    text = export_text([row("Sun shade A")]).replace(HEADER, HEADER + ",ROAS", 1).replace(
        '"$287.50"\n', '"$287.50",3.11\n'
    )
    frame = read_export(write(tmp_path / "Products.csv", text))
    assert list(frame.columns) == list(REQUIRED_COLUMNS)
    assert frame.loc[0, "Products"] == "Sun shade A"
    assert frame.loc[0, "Sales(USD)"] == "$1,104.98"


def test_skips_metadata_lines_and_summary_rows(tmp_path):
    text = export_text(
        [row("Sun shade A"), "", 'Total,,,,"$1,104.98",,"36,849",410,"$355.18",46,12,"$287.50"'],
        preamble="Report: Products\nDate range: Oct 28, 2026\n",
    )
    frame = read_export(write(tmp_path / "Products.csv", text))
    assert frame["Products"].tolist() == ["Sun shade A"]


def test_missing_column_names_the_columns_picker(tmp_path):
    text = export_text([row("A")]).replace("NTB orders", "New orders")
    with pytest.raises(CSVValidationError, match="NTB orders.*Columns"):
        read_export(write(tmp_path / "Products.csv", text))


def test_not_a_products_export(tmp_path):
    with pytest.raises(CSVValidationError, match="not a Products page export"):
        read_export(write(tmp_path / "x.csv", "Campaign name,Date\nA,B\n"))


def test_combines_pages_in_order_and_checks_the_total(tmp_path):
    p1 = write(tmp_path / "p1.csv", export_text([row("A"), row("B")]))
    p2 = write(tmp_path / "p2.csv", export_text([row("C")]))
    export = combine_exports([p1, p2], expected_total=3)
    assert export.frame["Products"].tolist() == ["A", "B", "C"]
    assert export.rows_per_file == [2, 1]


def test_incomplete_export_is_refused(tmp_path):
    p1 = write(tmp_path / "p1.csv", export_text([row("A"), row("B")]))
    with pytest.raises(IncompleteExportError, match="2 row.*shows 463"):
        combine_exports([p1], expected_total=463)


def test_same_rows_on_two_pages_is_refused(tmp_path):
    p1 = write(tmp_path / "p1.csv", export_text([row("A"), row("B")]))
    p2 = write(tmp_path / "p2.csv", export_text([row("A"), row("B")]))
    with pytest.raises(CSVValidationError, match="more than one page"):
        combine_exports([p1, p2], expected_total=4)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$2,861.76", 2861.76),
        ("9.71%", 9.71),
        ("87,962", 87962.0),
        ("(45.00)", -45.0),
        ("-3", -3.0),
        ("—", None),
        ("-", None),
        ("", None),
    ],
)
def test_parse_number(value, expected):
    assert parse_number(value) == expected


def test_parse_number_rejects_text():
    with pytest.raises(ValueError):
        parse_number("Eligible")
