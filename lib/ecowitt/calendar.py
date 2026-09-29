"""Local public holidays, from the station's time zone (the person's own state, not a guess by the model)."""

from datetime import date, tzinfo

try:
    import holidays
except ImportError:  # optional: without it, holiday questions say they can't be answered
    holidays = None

# time zone -> (country, state) for the `holidays` package
REGIONS = {
    "Australia/Melbourne": ("AU", "VIC"), "Australia/Sydney": ("AU", "NSW"), "Australia/Brisbane": ("AU", "QLD"),
    "Australia/Adelaide": ("AU", "SA"), "Australia/Perth": ("AU", "WA"), "Australia/Hobart": ("AU", "TAS"),
    "Australia/Darwin": ("AU", "NT"), "Australia/Canberra": ("AU", "ACT"),
}


class PublicHolidays:
    def __init__(self, tz: tzinfo):
        region = REGIONS.get(getattr(tz, "key", ""))
        self.region = region
        self.available = bool(holidays and region)
        self._calendar = holidays.country_holidays(region[0], subdiv=region[1]) if self.available else {}

    def name(self, day: date) -> str | None:
        """The holiday's name (e.g. "Australia Day"), or None on an ordinary day."""
        return self._calendar.get(day)

    def label(self) -> str:
        return "-".join(self.region) if self.region else "unknown"
