from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import Column, Date, Integer, select, text

from app.config import REQUIRED_COLUMNS
from app.exceptions import DatabaseError, DataTransformationError
from app.products_csv import combine_exports

from .conftest import export_text, make_products_table, row, write

DAY = date(2026, 10, 28)


def frame(tmp_path, *rows):
    return combine_exports([write(tmp_path / "p.csv", export_text(list(rows)))]).frame


def stored(table, day=DAY):
    with table.engine.connect() as connection:
        result = connection.execute(select(table.table).where(table._date_criteria(day)))
        return pd.DataFrame(result.mappings().all())


def test_numbers_are_cleaned_for_numeric_columns(tmp_path, products_table):
    records = products_table.to_records(frame(tmp_path, row("A")), DAY)
    record = records[0]
    assert record["Date"] == "2026-10-28"
    assert record["Sales(USD)"] == 1104.98
    assert record["Conversion rate"] == 11.24
    assert record["Impressions"] == 36849 and isinstance(record["Impressions"], int)
    assert record["Sponsored Products eligibility"] == "Eligible"


def test_text_columns_keep_the_exported_text(tmp_path):
    table = make_products_table(numeric=False)
    record = table.to_records(frame(tmp_path, row("A")), DAY)[0]
    assert record["Sales(USD)"] == "$1,104.98"
    assert record["Impressions"] == "36,849"


def test_text_in_a_numeric_column_is_refused(tmp_path, products_table):
    with pytest.raises(DataTransformationError, match="Clicks"):
        products_table.to_records(frame(tmp_path, row("A", clicks="many")), DAY)


def test_replace_date_is_idempotent_and_leaves_other_dates(tmp_path, products_table):
    other = date(2026, 10, 27)
    products_table.replace_date(other, products_table.to_records(frame(tmp_path, row("Old")), other))
    records = products_table.to_records(frame(tmp_path, row("A"), row("B")), DAY)
    first = products_table.replace_date(DAY, records)
    second = products_table.replace_date(DAY, records)
    assert (first.deleted, first.inserted) == (0, 2)
    assert (second.deleted, second.inserted) == (2, 2)
    assert sorted(stored(products_table)["Products"]) == ["A", "B"]
    assert stored(products_table, other)["Products"].tolist() == ["Old"]


def test_date_column_of_type_date(tmp_path):
    table = make_products_table(date_type=Date)
    table.replace_date(DAY, table.to_records(frame(tmp_path, row("A")), DAY))
    assert table.count_for_date(DAY) == 1


def test_refuses_a_delete_only_write(products_table):
    with pytest.raises(ValueError, match="zero records"):
        products_table.replace_date(DAY, [])


def test_failed_insert_rolls_back_the_delete(tmp_path, products_table):
    products_table.replace_date(DAY, products_table.to_records(frame(tmp_path, row("Kept")), DAY))
    records = products_table.to_records(frame(tmp_path, row("A"), row("B")), DAY)
    records[1]["Products"] = None
    records[1]["Status"] = object()  # the driver rejects it after the DELETE has run
    with pytest.raises(Exception):
        products_table.replace_date(DAY, records)
    assert stored(products_table)["Products"].tolist() == ["Kept"]


def test_missing_key_column_is_reported():
    with pytest.raises(DatabaseError, match="missing column.*Products"):
        make_products_table(omit={"Products"})


def test_export_column_the_table_lacks_is_skipped(tmp_path):
    # Production powerbidb.Products has no "Conversion rate" column.
    table = make_products_table(omit={"Conversion rate"})
    assert "Conversion rate" not in table.written_columns
    records = table.to_records(frame(tmp_path, row("A")), DAY)
    assert "Conversion rate" not in records[0]
    table.replace_date(DAY, records)
    assert table.count_for_date(DAY) == 1


def test_required_extra_column_blocks_the_load():
    with pytest.raises(DatabaseError, match="NOT NULL"):
        make_products_table(extra=[Column("Marketplace", Integer, nullable=False)])


def test_text_date_column_in_another_format_is_refused(tmp_path):
    table = make_products_table()
    with table.engine.begin() as connection:
        connection.execute(text("INSERT INTO Products (Date, Products) VALUES ('28-10-2026', 'x')"))
    with pytest.raises(DatabaseError, match="DB_DATE_FORMAT"):
        table.check_date_format()


def test_written_columns_are_date_plus_the_export():
    assert make_products_table().written_columns == ("Date", *REQUIRED_COLUMNS)
