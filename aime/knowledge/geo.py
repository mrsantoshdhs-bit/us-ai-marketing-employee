"""US geography: states, FIPS codes, and conservative location parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
US_REGIONS: dict[str, str] = {**STATES, "DC": "District of Columbia"}

STATE_FIPS: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}

# Default research order (roughly by business population) when no Census data is loaded.
STATE_PRIORITY: list[str] = [
    "CA", "TX", "FL", "NY", "PA", "IL", "OH", "GA", "NC", "MI",
    "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MD", "MO", "WI",
    "CO", "MN", "SC", "AL", "LA", "KY", "OR", "OK", "CT", "UT",
    "IA", "NV", "AR", "MS", "KS", "NM", "NE", "ID", "WV", "HI",
    "NH", "ME", "RI", "MT", "DE", "SD", "ND", "AK", "VT", "WY",
]
assert len(STATE_PRIORITY) == 50 and set(STATE_PRIORITY) == set(STATES)

CITY_TO_STATE: dict[str, str] = {
    "new york city": "NY", "new york": "NY", "nyc": "NY", "brooklyn": "NY", "manhattan": "NY",
    "san francisco": "CA", "bay area": "CA", "silicon valley": "CA", "los angeles": "CA",
    "san diego": "CA", "san jose": "CA", "palo alto": "CA", "mountain view": "CA", "menlo park": "CA",
    "oakland": "CA", "berkeley": "CA", "santa monica": "CA", "irvine": "CA", "sacramento": "CA",
    "san mateo": "CA", "redwood city": "CA", "sunnyvale": "CA", "santa clara": "CA", "cupertino": "CA",
    "emeryville": "CA", "pasadena": "CA", "culver city": "CA", "seattle": "WA", "bellevue": "WA",
    "redmond": "WA", "austin": "TX", "dallas": "TX", "houston": "TX", "san antonio": "TX",
    "fort worth": "TX", "boston": "MA", "somerville": "MA", "chicago": "IL", "denver": "CO",
    "boulder": "CO", "atlanta": "GA", "miami": "FL", "tampa": "FL", "orlando": "FL",
    "jacksonville": "FL", "philadelphia": "PA", "pittsburgh": "PA", "phoenix, az": "AZ",
    "scottsdale": "AZ", "tempe": "AZ", "salt lake city": "UT", "lehi": "UT", "provo": "UT",
    "portland, or": "OR", "minneapolis": "MN", "detroit": "MI", "ann arbor": "MI", "nashville": "TN",
    "raleigh": "NC", "durham, nc": "NC", "charlotte, nc": "NC", "washington dc": "DC", "washington, dc": "DC",
    "washington d.c.": "DC", "baltimore": "MD", "columbus": "OH", "cleveland": "OH",
    "cincinnati": "OH", "indianapolis": "IN", "kansas city": "MO", "st. louis": "MO",
    "st louis": "MO", "las vegas": "NV", "new orleans": "LA", "milwaukee": "WI", "omaha": "NE",
    "boise": "ID", "honolulu": "HI", "anchorage": "AK", "albuquerque": "NM", "louisville": "KY",
    "hoboken": "NJ", "jersey city": "NJ", "stamford": "CT", "new haven": "CT", "providence": "RI",
    "des moines": "IA", "oklahoma city": "OK", "tulsa": "OK", "little rock": "AR",
}

NON_US_MARKERS: list[str] = [
    "london", "united kingdom", "uk", "england", "scotland", "berlin", "munich", "germany",
    "canada", "toronto", "vancouver", "montreal", "ottawa", "europe", "eu", "emea", "india",
    "bangalore", "bengaluru", "hyderabad", "pune", "australia", "sydney", "melbourne", "singapore",
    "paris", "france", "amsterdam", "netherlands", "spain", "barcelona", "madrid", "israel",
    "tel aviv", "brazil", "mexico", "japan", "tokyo", "dublin", "ireland", "zurich", "switzerland",
    "stockholm", "sweden", "poland", "warsaw", "portugal", "lisbon", "latam", "apac", "copenhagen",
    "denmark", "oslo", "norway", "helsinki", "finland", "vienna", "austria", "prague", "estonia",
    "argentina", "colombia", "philippines", "nigeria", "kenya", "south africa", "dubai", "uae",
]

_US_MARKER_RE = re.compile(
    r"(?<![A-Za-z])(?:USA|U\.S\.A?\.?|US)(?![A-Za-z])"
    r"|(?<![a-z])(?:united states|us[- ]only|us[- ]based|remote[ (,/-]*us)(?![a-z])",
)
_DEGREE_PREFIX_RE = re.compile(r"(?:B\.?S|B\.?A|M\.?S)\.?/\s*$")
_STATE_CODE_RE = re.compile(r"(?:,|\(|/|\||;|-|–)\s*([A-Z]{2})(?=\s*(?:$|[,)|/;\-–]|\s|\.))")
_NON_US_RE = re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(m) for m in NON_US_MARKERS) + r")(?![a-z])")
_STATE_NAMES_SORTED = sorted(((name.lower(), code) for code, name in US_REGIONS.items()), key=lambda x: -len(x[0]))
_CITIES_SORTED = sorted(CITY_TO_STATE.items(), key=lambda x: -len(x[0]))


@dataclass
class Location:
    is_us: bool | None
    state: str | None = None
    city: str | None = None
    remote: bool = False
    evidence: list[str] = field(default_factory=list)


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.upper() in US_REGIONS:
        return v.upper()
    lookup = {name.lower(): code for code, name in US_REGIONS.items()}
    return lookup.get(v.lower())


def parse_location(text: str | None) -> Location:
    """Decide conservatively whether text describes a US location.

    Returns is_us=True only on explicit US evidence (state, US city, or "US"),
    False when only non-US places appear, and None when unknown.
    """
    if not text:
        return Location(is_us=None)
    lower = text.lower()
    found: list[tuple[int, str, str | None, str]] = []  # (pos, state, city, evidence)

    for match in _STATE_CODE_RE.finditer(text):
        code = match.group(1)
        if code not in US_REGIONS:
            continue
        if _DEGREE_PREFIX_RE.search(text[max(0, match.start() - 6): match.start() + 1]):
            continue  # "BS/MS degree" is not Mississippi
        found.append((match.start(1), code, None, f"state code {code}"))

    masked = lower
    for city, code in _CITIES_SORTED:
        for m in re.finditer(r"(?<![a-z])" + re.escape(city) + r"(?![a-z])", masked):
            found.append((m.start(), code, city.title().split(",")[0], f"city {city}"))
            masked = masked[: m.start()] + " " * len(city) + masked[m.end():]

    for name, code in _STATE_NAMES_SORTED:
        for m in re.finditer(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", masked):
            found.append((m.start(), code, None, f"state {name}"))
            masked = masked[: m.start()] + " " * len(name) + masked[m.end():]

    us_marker = bool(_US_MARKER_RE.search(text) or _US_MARKER_RE.search(lower))
    remote = "remote" in lower
    if found:
        found.sort(key=lambda f: f[0])
        _, state, city, ev = found[0]
        return Location(is_us=True, state=state, city=city, remote=remote, evidence=[f[3] for f in found])
    if us_marker:
        return Location(is_us=True, remote=remote, evidence=["explicit US marker"])
    if _NON_US_RE.search(lower):
        return Location(is_us=False, remote=remote, evidence=["non-US location only"])
    return Location(is_us=None, remote=remote)
