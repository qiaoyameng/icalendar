"""This package contains all functionality for timezones."""

from .tzid import is_utc, tzid_from_dt, tzid_from_tzinfo, tzids_from_tzinfo
from .tzp import TZP, TimezoneContext, timezone_context

tzp = TZP()


def use_pytz():
    """Use pytz as the implementation that looks up and creates timezones."""
    tzp.use_pytz()


def use_zoneinfo():
    """Use zoneinfo as the implementation that looks up and creates timezones."""
    tzp.use_zoneinfo()


__all__ = [
    "TZP",
    "TimezoneContext",
    "is_utc",
    "timezone_context",
    "tzid_from_dt",
    "tzid_from_tzinfo",
    "tzids_from_tzinfo",
    "tzp",
    "use_pytz",
    "use_zoneinfo",
]
