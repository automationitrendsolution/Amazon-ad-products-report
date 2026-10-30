"""Which date a run loads.

The Products page's **Today** and **Yesterday** presets follow the Amazon
advertiser account's calendar (America/Los_Angeles for US accounts), not the
server's or IST.  The date each load is stored under is computed in that same
calendar, exactly as the manual upload script did with ``US/Pacific``:

    run at 15:00 IST = 02:30 Los Angeles  -> Today = that LA date
    run at 14:00 IST = 01:30 Los Angeles  -> Yesterday = the LA day that just ended

A US day starts at 12:30 IST (13:30 IST in US winter time), so by 14:00 IST
the previous US day is always complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import PERIODS, Settings
from .exceptions import ConfigurationError
from .timeutils import now_in


@dataclass(frozen=True)
class ResolvedPeriod:
    """One date preset on the Products page and the date it stands for."""

    #: "today" or "yesterday".
    period: str
    report_date: date

    @property
    def preset_label(self) -> str:
        """The preset's text on the page: "Today" / "Yesterday"."""
        return self.period.capitalize()


def resolve_period(settings: Settings, period: str, *, reference: datetime | None = None) -> ResolvedPeriod:
    """The date ``period`` covers right now, in the Amazon account's calendar."""
    if period not in PERIODS:
        raise ConfigurationError(f"period must be one of {', '.join(PERIODS)}; got {period!r}")
    account_today = now_in(settings.amazon_account_timezone, reference=reference).date()
    offset = 0 if period == "today" else 1
    return ResolvedPeriod(period, account_today - timedelta(days=offset))
