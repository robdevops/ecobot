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

    def problem(self) -> str:
        """Why holidays can't be answered (for the person, via the model), or "" if they can."""
        if self.available:
            return ""
        if self.region:
            return ("The 'holidays' Python package isn't installed on the bot's server, so public holidays can't be "
                    "looked up. The owner needs to run: pip install holidays (or pip install -r requirements.txt), "
                    "then restart the bot. Say exactly that.")
        return "Public holidays aren't known for this time zone; say so."
