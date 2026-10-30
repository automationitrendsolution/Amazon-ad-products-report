"""MySQL persistence for ``powerbidb.Products`` -- and nothing else.

The table already exists; it is reflected, never created or altered.  A load
replaces one date in **one transaction** on one connection:

    BEGIN
      DELETE FROM Products WHERE Date = <date>
      INSERT ... the merged page exports ...
      SELECT COUNT(*) FROM Products WHERE Date = <date>   -- must equal inserted
    COMMIT            -- or ROLLBACK, leaving the previous rows intact

Power BI therefore never sees a half-loaded day.  Re-running the same load
converges on the same rows, so a retry after a lost connection is safe and
cannot duplicate data.

Values are written in the form each live column expects: numeric columns get
numbers (``$2,861.76`` -> 2861.76, ``9.71%`` -> 9.71), text columns keep the
exported text, exactly as the manual upload script stored it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    and_,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import DATE_COLUMN, PRODUCTS_TABLE, REQUIRED_COLUMNS, DatabaseConfig
from .exceptions import DatabaseError, DataTransformationError
from .logging_config import get_logger
from .products_csv import parse_number

logger = get_logger("database")

INSERT_CHUNK_SIZE = 500

#: MySQL errors a second attempt can plausibly get past.
TRANSIENT_MYSQL_ERRNOS = frozenset({1205, 1213, 2003, 2006, 2013, 2055})


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    error = DatabaseError(message)
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    error.transient = bool(
        getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS
    )
    return error


@dataclass(frozen=True)
class WriteResult:
    table: str
    report_date: date
    deleted: int
    inserted: int


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Pooled engine.  The URL is never logged with its password."""
    try:
        return create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not create engine for {config.safe_url}: {exc}") from exc


class ProductsTable:
    """Schema checks, value conversion and date replacement for ``Products``."""

    def __init__(self, engine: Engine, *, date_format: str = "%Y-%m-%d") -> None:
        self.engine = engine
        self.table_name = PRODUCTS_TABLE
        self.date_column = DATE_COLUMN
        self.date_format = date_format
        self._table: Table | None = None

    # -- schema -------------------------------------------------------------

    def check_connection(self) -> None:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Database connection verified")

    def reflect(self) -> Table:
        """Load the live definition.  The table must already exist."""
        try:
            exists = inspect(self.engine).has_table(self.table_name)
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not inspect the database: {exc}", exc) from exc
        if not exists:
            raise DatabaseError(f"Table {self.table_name!r} does not exist. It is never created automatically.")
        try:
            self._table = Table(self.table_name, MetaData(), autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not reflect {self.table_name!r}: {exc}", exc) from exc
        logger.info("Reflected %s with %d column(s)", self.table_name, len(self._table.columns))
        return self._table

    @property
    def table(self) -> Table:
        return self._table if self._table is not None else self.reflect()

    @property
    def export_columns(self) -> tuple[str, ...]:
        """Export columns the table has.

        The others (e.g. ``Conversion rate``) are left out, as the manual
        products_secondary_upload.py script did.
        """
        live = self.table.c
        return tuple(name for name in REQUIRED_COLUMNS if name in live)

    @property
    def written_columns(self) -> tuple[str, ...]:
        return (self.date_column, *self.export_columns)

    def validate_schema(self) -> None:
        """The table must hold every written column; extras must be optional.

        Raises:
            DatabaseError: A written column is missing from the table, or the
                table has a required column the export cannot fill.
        """
        live = {c.name: c for c in self.table.columns}
        missing = [c for c in (self.date_column, "Products") if c not in live]
        if missing:
            raise DatabaseError(f"{self.table_name} is missing column(s) the export writes: {', '.join(missing)}")
        skipped = [c for c in REQUIRED_COLUMNS if c not in live]
        if skipped:
            logger.info("%s has no column(s) %s; those export values are not stored", self.table_name,
                        ", ".join(skipped))
        blocking, optional = [], []
        for name, column in live.items():
            if name in self.written_columns:
                continue
            generated = column.primary_key and column.autoincrement in (True, "auto")
            has_default = column.server_default is not None or column.default is not None
            if generated or column.nullable or has_default:
                optional.append(name)
            else:
                blocking.append(name)
        if blocking:
            raise DatabaseError(
                f"{self.table_name} has NOT NULL column(s) without a default that the export "
                f"does not fill: {', '.join(blocking)}"
            )
        if optional:
            logger.info("%s: columns left to their defaults: %s", self.table_name, ", ".join(optional))
        logger.info(
            "Schema validated: %d column(s) match %s (%s type: %s)",
            len(self.written_columns),
            self.table_name,
            self.date_column,
            self.table.c[self.date_column].type,
        )

    # -- dates ----------------------------------------------------------------

    @property
    def _date_kind(self) -> str:
        column_type = self.table.c[self.date_column].type
        if isinstance(column_type, DateTime):
            return "datetime"
        if isinstance(column_type, Date):
            return "date"
        return "text"

    def date_value(self, day: date) -> object:
        """``day`` as stored in the date column."""
        kind = self._date_kind
        if kind == "datetime":
            return datetime(day.year, day.month, day.day)
        if kind == "date":
            return day
        return day.strftime(self.date_format)

    def _date_criteria(self, day: date):
        column = self.table.c[self.date_column]
        if self._date_kind == "datetime":
            # A range, so rows stored with a time of day are still matched.
            return and_(column >= self.date_value(day), column < self.date_value(day + timedelta(days=1)))
        return column == self.date_value(day)

    def check_date_format(self, sample_size: int = 50) -> None:
        """For a text date column, existing values must use DB_DATE_FORMAT.

        Otherwise the DELETE would miss the rows already stored for a date and
        the INSERT would duplicate them.

        Raises:
            DatabaseError: No sampled value parses with DB_DATE_FORMAT.
        """
        if self._date_kind != "text":
            return
        column = self.table.c[self.date_column]
        try:
            with self.engine.connect() as connection:
                values = [
                    row[0]
                    for row in connection.execute(select(column).where(column.is_not(None)).limit(sample_size))
                ]
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not sample {self.date_column}: {exc}", exc) from exc
        if not values:
            logger.info("%s is empty; dates will be written as %s", self.table_name, self.date_format)
            return

        def parses(value: object) -> bool:
            try:
                datetime.strptime(str(value).strip(), self.date_format)
                return True
            except ValueError:
                return False

        bad = [v for v in values if not parses(v)]
        if len(bad) == len(values):
            raise DatabaseError(
                f"Existing {self.table_name}.{self.date_column} values (e.g. {bad[:3]}) do not match "
                f"DB_DATE_FORMAT={self.date_format!r}. Set DB_DATE_FORMAT to the stored format; "
                "otherwise old rows would not be replaced and duplicates would appear."
            )
        if bad:
            logger.warning(
                "%d of %d sampled %s.%s values do not match %r, e.g. %s",
                len(bad), len(values), self.table_name, self.date_column, self.date_format, bad[:3],
            )

    # -- values ---------------------------------------------------------------

    def to_records(self, frame: pd.DataFrame, day: date) -> list[dict[str, object]]:
        """Export rows -> driver-ready dicts for ``day``.

        Raises:
            DataTransformationError: A numeric column received text such as
                ``Eligible`` (the columns are in a different order or meaning).
        """
        kinds = {}
        columns = self.export_columns
        for name in columns:
            column_type = self.table.c[name].type
            if isinstance(column_type, (Integer, BigInteger, SmallInteger)):
                kinds[name] = "int"
            # Float is not a Numeric subclass since SQLAlchemy 2.1; MySQL DOUBLE is a Float.
            elif isinstance(column_type, (Numeric, Float)):
                kinds[name] = "number"
            else:
                kinds[name] = "text"

        stored_date = self.date_value(day)
        bad: dict[str, list[str]] = {}
        records: list[dict[str, object]] = []
        for row in frame.loc[:, list(columns)].itertuples(index=False, name=None):
            record: dict[str, object] = {self.date_column: stored_date}
            for name, raw in zip(columns, row):
                value = "" if raw is None else str(raw).strip()
                if kinds[name] == "text":
                    record[name] = value or None
                    continue
                try:
                    number = parse_number(value)
                except ValueError:
                    bad.setdefault(name, []).append(value)
                    continue
                if number is not None and kinds[name] == "int":
                    number = int(round(number))
                record[name] = number
            records.append(record)
        if bad:
            details = "; ".join(f"{name}: {values[:3]}" for name, values in bad.items())
            raise DataTransformationError(f"Non-numeric value(s) for numeric column(s) in {self.table_name}: {details}")
        return records

    # -- writes -----------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT on one connection, ROLLBACK on any error."""
        connection = self.engine.connect()
        try:
            trans = connection.begin()
            try:
                yield connection
            except Exception as exc:
                try:
                    trans.rollback()
                except SQLAlchemyError as rollback_exc:
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.error("ROLLBACK after error: %s: %s", type(exc).__name__, exc)
                raise
            trans.commit()
        finally:
            connection.close()

    def replace_date(self, day: date, records: Sequence[dict[str, object]]) -> WriteResult:
        """DELETE + INSERT + COUNT for ``day``, atomically.

        Raises:
            ValueError: No records, or records for another date.
            DatabaseError: Any database failure -- rolled back first.
        """
        if not records:
            raise ValueError(f"{self.table_name}: zero records; refusing to delete data")
        stored = self.date_value(day)
        stray = {r[self.date_column] for r in records} - {stored}
        if stray:
            raise ValueError(f"{self.table_name}: records contain dates other than {stored}: {sorted(map(str, stray))}")

        table = self.table
        criteria = self._date_criteria(day)
        try:
            with self.transaction() as connection:
                deleted = connection.execute(delete(table).where(criteria)).rowcount
                inserted = 0
                for start in range(0, len(records), INSERT_CHUNK_SIZE):
                    chunk = list(records[start : start + INSERT_CHUNK_SIZE])
                    connection.execute(insert(table), chunk)
                    inserted += len(chunk)
                present = connection.execute(select(func.count()).select_from(table).where(criteria)).scalar_one()
                if present != inserted:
                    raise DatabaseError(
                        f"After insert {present} row(s) exist in {self.table_name} for {day}, "
                        f"expected {inserted}; rolling back"
                    )
        except SQLAlchemyError as exc:
            raise _database_error(f"Load for {day} failed and was rolled back: {exc}", exc) from exc
        logger.info("Committed %s: %s %d deleted / %d inserted", day, self.table_name, deleted, inserted)
        return WriteResult(self.table_name, day, max(int(deleted), 0), inserted)

    def count_for_date(self, day: date) -> int:
        try:
            with self.engine.connect() as connection:
                return int(
                    connection.execute(
                        select(func.count()).select_from(self.table).where(self._date_criteria(day))
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise _database_error(f"Could not count rows: {exc}", exc) from exc

    def dispose(self) -> None:
        self.engine.dispose()


def build_products_table(config: DatabaseConfig, *, date_format: str, echo: bool = False) -> ProductsTable:
    """Connect, reflect and validate the live table before anything is downloaded."""
    engine = create_db_engine(config, echo=echo)
    try:
        table = ProductsTable(engine, date_format=date_format)
        table.check_connection()
        table.reflect()
        table.validate_schema()
        table.check_date_format()
    except Exception:
        engine.dispose()
        raise
    return table
