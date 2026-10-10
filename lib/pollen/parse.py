"""Melbourne grass pollen and thunderstorm asthma risk, read from melbournepollen.com.au (AirHealth / University of Melbourne:
grass pollen from pollen-trap data; the asthma risk is the official epidemic thunderstorm asthma forecast of the Victorian
Department of Health and the Bureau of Meteorology, 1 Oct - 31 Dec). The site has no API, so the homepage is parsed."""

import re
from datetime import datetime
from html.parser import HTMLParser
from itertools import pairwise

LEVELS = ("Low", "Moderate", "High")   # the site's scale; "No data" is not a level: it reads as if there were no forecast
LEVEL_EMOJI = {"Low": "🟢", "Moderate": "🟠", "High": "🔴"}
DISTRICTS = {"Central", "East Gippsland", "Mallee", "North Central", "North East",
             "Northern Country", "South West", "West and South Gippsland", "Wimmera"}


class _TextLines(HTMLParser):
    """The visible text of an HTML page, one text node per line."""

    def __init__(self):
        super().__init__()
        self.lines, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and not self._skip:
            self.lines.append(text)


def _section(lines: list[str], heading: str, stops: list[str]) -> list[str]:
    """Lines after the first line equal to `heading`, up to a line starting with a stop."""
    try:
        i = lines.index(heading)
    except ValueError:
        return []
    out = []
    for line in lines[i + 1:]:
        if any(line.startswith(s) for s in stops):
            break
        out.append(line)
    return out


def _district_levels(lines: list[str]) -> dict[str, str]:
    return {a: b for a, b in pairwise(lines) if a in DISTRICTS and b in LEVELS}


def _last_updated(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if line.startswith("Last updated"):
            value = line.split(":", 1)[1].strip() if ":" in line else ""
            return value or (lines[i + 1] if i + 1 < len(lines) else None)
    return None


def parse_melbourne_pollen(html: str) -> dict:
    """{"melbourne_grass": level | None, "melbourne_date": date | None, "district_grass": {district: level},
    "thunderstorm_asthma": {district: level}, "asthma_updated": str | None}"""
    parser = _TextLines()
    parser.feed(html)
    lines = parser.lines

    # Today's Melbourne forecast: the elements with id="plevel" / id="pdate" (fallback: the first "pollen-level level-X" class)
    m_level = (re.search(r'id="plevel"[^>]*>\s*([^<]+?)\s*<', html)
               or re.search(r'class="pollen-level level-\w+"[^>]*>\s*([^<]+?)\s*<', html))
    level = m_level.group(1) if m_level and m_level.group(1) in LEVELS else None

    date = None
    if m_date := re.search(r'id="pdate"[^>]*>\s*([^<]+?)\s*<', html):
        try:
            date = datetime.strptime(m_date.group(1), "%A, %B %d, %Y").date()
        except ValueError:
            pass

    grass = _section(lines, "Victorian District Grass Pollen Forecast", ["Thunderstorm Asthma Forecast"])
    asthma = _section(lines, "Thunderstorm Asthma Forecast", ["Note:", "Recent News"])
    after = lines[lines.index("Thunderstorm Asthma Forecast"):] if "Thunderstorm Asthma Forecast" in lines else []
    return {
        "melbourne_grass": level,
        "melbourne_date": date,
        "district_grass": _district_levels(grass),
        "thunderstorm_asthma": _district_levels(asthma),
        "asthma_updated": _last_updated(after),  # the "Last updated" line sits just after the "Note:" paragraph
    }
