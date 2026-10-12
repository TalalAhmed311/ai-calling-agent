"""Map US addresses / phones to IANA timezones for local call windows."""

from __future__ import annotations

import re
from datetime import datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

# Primary business timezone per state / DC. Multi-zone states use the
# majority zone; ZIP / city overrides can refine later.
STATE_TIMEZONES: dict[str, str] = {
    "AL": "America/Chicago",
    "AK": "America/Anchorage",
    "AZ": "America/Phoenix",
    "AR": "America/Chicago",
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DE": "America/New_York",
    "DC": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "ID": "America/Boise",
    "IL": "America/Chicago",
    "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "KY": "America/New_York",
    "LA": "America/Chicago",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MA": "America/New_York",
    "MI": "America/Detroit",
    "MN": "America/Chicago",
    "MS": "America/Chicago",
    "MO": "America/Chicago",
    "MT": "America/Denver",
    "NE": "America/Chicago",
    "NV": "America/Los_Angeles",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NY": "America/New_York",
    "NC": "America/New_York",
    "ND": "America/Chicago",
    "OH": "America/New_York",
    "OK": "America/Chicago",
    "OR": "America/Los_Angeles",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "UT": "America/Denver",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WA": "America/Los_Angeles",
    "WV": "America/New_York",
    "WI": "America/Chicago",
    "WY": "America/Denver",
}

STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

# NANP area code → IANA (majority zone). Used only when address is missing.
AREA_CODE_TZ: dict[str, str] = {
    # Eastern
    "201": "America/New_York", "202": "America/New_York", "203": "America/New_York",
    "212": "America/New_York", "215": "America/New_York", "216": "America/New_York",
    "301": "America/New_York", "302": "America/New_York", "305": "America/New_York",
    "313": "America/Detroit", "315": "America/New_York", "321": "America/New_York",
    "347": "America/New_York", "351": "America/New_York", "352": "America/New_York",
    "386": "America/New_York", "401": "America/New_York", "404": "America/New_York",
    "407": "America/New_York", "410": "America/New_York", "412": "America/New_York",
    "413": "America/New_York", "419": "America/New_York", "434": "America/New_York",
    "440": "America/New_York", "443": "America/New_York", "470": "America/New_York",
    "475": "America/New_York", "478": "America/New_York", "484": "America/New_York",
    "508": "America/New_York", "513": "America/New_York", "516": "America/New_York",
    "517": "America/Detroit", "518": "America/New_York", "540": "America/New_York",
    "551": "America/New_York", "561": "America/New_York", "567": "America/New_York",
    "570": "America/New_York", "571": "America/New_York", "585": "America/New_York",
    "586": "America/Detroit", "603": "America/New_York", "607": "America/New_York",
    "609": "America/New_York", "610": "America/New_York", "614": "America/New_York",
    "616": "America/Detroit", "617": "America/New_York", "631": "America/New_York",
    "646": "America/New_York", "678": "America/New_York", "680": "America/New_York",
    "681": "America/New_York", "703": "America/New_York", "704": "America/New_York",
    "706": "America/New_York", "716": "America/New_York", "717": "America/New_York",
    "718": "America/New_York", "724": "America/New_York", "727": "America/New_York",
    "732": "America/New_York", "734": "America/Detroit", "740": "America/New_York",
    "743": "America/New_York", "754": "America/New_York", "757": "America/New_York",
    "762": "America/New_York", "770": "America/New_York", "772": "America/New_York",
    "774": "America/New_York", "781": "America/New_York", "786": "America/New_York",
    "802": "America/New_York", "803": "America/New_York", "804": "America/New_York",
    "810": "America/Detroit", "813": "America/New_York", "814": "America/New_York",
    "828": "America/New_York", "843": "America/New_York", "845": "America/New_York",
    "848": "America/New_York", "854": "America/New_York", "856": "America/New_York",
    "857": "America/New_York", "859": "America/New_York", "860": "America/New_York",
    "862": "America/New_York", "863": "America/New_York", "864": "America/New_York",
    "865": "America/New_York", "878": "America/New_York", "904": "America/New_York",
    "908": "America/New_York", "910": "America/New_York", "912": "America/New_York",
    "914": "America/New_York", "917": "America/New_York", "919": "America/New_York",
    "929": "America/New_York", "934": "America/New_York", "941": "America/New_York",
    "947": "America/Detroit", "954": "America/New_York", "959": "America/New_York",
    "973": "America/New_York", "978": "America/New_York", "980": "America/New_York",
    "984": "America/New_York", "989": "America/Detroit",
    # Central
    "205": "America/Chicago", "210": "America/Chicago", "214": "America/Chicago",
    "217": "America/Chicago", "218": "America/Chicago", "219": "America/Chicago",
    "225": "America/Chicago", "228": "America/Chicago", "251": "America/Chicago",
    "254": "America/Chicago", "256": "America/Chicago", "262": "America/Chicago",
    "281": "America/Chicago", "309": "America/Chicago", "312": "America/Chicago",
    "314": "America/Chicago", "316": "America/Chicago", "318": "America/Chicago",
    "319": "America/Chicago", "320": "America/Chicago", "325": "America/Chicago",
    "330": "America/New_York", "331": "America/Chicago", "334": "America/Chicago",
    "337": "America/Chicago", "346": "America/Chicago", "361": "America/Chicago",
    "364": "America/Chicago", "405": "America/Chicago", "409": "America/Chicago",
    "414": "America/Chicago", "417": "America/Chicago", "430": "America/Chicago",
    "432": "America/Chicago", "469": "America/Chicago", "479": "America/Chicago",
    "501": "America/Chicago", "502": "America/New_York", "504": "America/Chicago",
    "507": "America/Chicago", "512": "America/Chicago", "515": "America/Chicago",
    "563": "America/Chicago", "573": "America/Chicago", "580": "America/Chicago",
    "601": "America/Chicago", "608": "America/Chicago", "612": "America/Chicago",
    "615": "America/Chicago", "618": "America/Chicago", "620": "America/Chicago",
    "630": "America/Chicago", "636": "America/Chicago", "641": "America/Chicago",
    "651": "America/Chicago", "660": "America/Chicago", "662": "America/Chicago",
    "682": "America/Chicago", "701": "America/Chicago", "708": "America/Chicago",
    "712": "America/Chicago", "713": "America/Chicago", "715": "America/Chicago",
    "731": "America/Chicago", "737": "America/Chicago", "763": "America/Chicago",
    "769": "America/Chicago", "773": "America/Chicago", "785": "America/Chicago",
    "806": "America/Chicago", "815": "America/Chicago", "816": "America/Chicago",
    "817": "America/Chicago", "830": "America/Chicago", "832": "America/Chicago",
    "847": "America/Chicago", "870": "America/Chicago", "872": "America/Chicago",
    "901": "America/Chicago", "903": "America/Chicago", "913": "America/Chicago",
    "915": "America/Denver", "918": "America/Chicago", "920": "America/Chicago",
    "936": "America/Chicago", "940": "America/Chicago", "952": "America/Chicago",
    "956": "America/Chicago", "972": "America/Chicago", "979": "America/Chicago",
    "985": "America/Chicago",
    # Mountain
    "303": "America/Denver", "307": "America/Denver", "385": "America/Denver",
    "406": "America/Denver", "435": "America/Denver", "480": "America/Phoenix",
    "505": "America/Denver", "520": "America/Phoenix", "575": "America/Denver",
    "602": "America/Phoenix", "623": "America/Phoenix", "719": "America/Denver",
    "720": "America/Denver", "801": "America/Denver", "928": "America/Phoenix",
    "970": "America/Denver",
    # Pacific
    "206": "America/Los_Angeles", "208": "America/Boise", "209": "America/Los_Angeles",
    "213": "America/Los_Angeles", "253": "America/Los_Angeles", "310": "America/Los_Angeles",
    "323": "America/Los_Angeles", "341": "America/Los_Angeles", "360": "America/Los_Angeles",
    "408": "America/Los_Angeles", "415": "America/Los_Angeles", "424": "America/Los_Angeles",
    "425": "America/Los_Angeles", "442": "America/Los_Angeles", "458": "America/Los_Angeles",
    "503": "America/Los_Angeles", "509": "America/Los_Angeles", "510": "America/Los_Angeles",
    "530": "America/Los_Angeles", "541": "America/Los_Angeles", "559": "America/Los_Angeles",
    "562": "America/Los_Angeles", "619": "America/Los_Angeles", "626": "America/Los_Angeles",
    "628": "America/Los_Angeles", "650": "America/Los_Angeles", "657": "America/Los_Angeles",
    "661": "America/Los_Angeles", "669": "America/Los_Angeles", "702": "America/Los_Angeles",
    "707": "America/Los_Angeles", "714": "America/Los_Angeles", "725": "America/Los_Angeles",
    "747": "America/Los_Angeles", "760": "America/Los_Angeles", "775": "America/Los_Angeles",
    "805": "America/Los_Angeles", "818": "America/Los_Angeles", "820": "America/Los_Angeles",
    "831": "America/Los_Angeles", "858": "America/Los_Angeles", "909": "America/Los_Angeles",
    "916": "America/Los_Angeles", "925": "America/Los_Angeles", "949": "America/Los_Angeles",
    "951": "America/Los_Angeles", "971": "America/Los_Angeles",
    # Alaska / Hawaii
    "907": "America/Anchorage", "808": "Pacific/Honolulu",
}

_STATE_IN_ADDR = re.compile(
    r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b|\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b",
    re.I,
)
_ZIP_IN_ADDR = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def normalize_state(token: str | None) -> str | None:
    if not token:
        return None
    t = token.strip()
    if len(t) == 2:
        up = t.upper()
        return up if up in STATE_TIMEZONES else None
    return STATE_NAMES.get(t.lower())


def parse_state_from_address(address: str | None) -> str | None:
    text = (address or "").strip()
    if not text:
        return None
    m = _STATE_IN_ADDR.search(text)
    if m:
        return normalize_state(m.group(1) or m.group(2))
    # Fallback: last comma segment may be "Virginia" or "VA"
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts:
        tail = parts[-1]
        # "VA 23220" or "Virginia"
        bits = tail.split()
        if bits:
            st = normalize_state(bits[0])
            if st:
                return st
        st = normalize_state(tail)
        if st:
            return st
    # Scan full state names inside address / query
    low = text.lower()
    for name, code in sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return code
    return None


def timezone_from_phone(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) < 10:
        return None
    return AREA_CODE_TZ.get(digits[:3])


def resolve_timezone(
    *,
    address: str | None = None,
    phone: str | None = None,
    fallback: str | None = None,
) -> str | None:
    """Best-effort IANA zone for a US business lead."""
    state = parse_state_from_address(address)
    if state and state in STATE_TIMEZONES:
        return STATE_TIMEZONES[state]
    tz = timezone_from_phone(phone)
    if tz:
        return tz
    return fallback


@lru_cache(maxsize=32)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _parse_hhmm(value: str, default: time) -> time:
    text = (value or "").strip()
    if not text:
        return default
    try:
        hh, mm = text.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except Exception:
        return default


def local_now(timezone_name: str) -> datetime:
    return datetime.now(_zone(timezone_name))


def is_within_local_window(
    timezone_name: str | None,
    *,
    start: str = "10:00",
    end: str = "14:00",
    weekdays_only: bool = True,
    now: datetime | None = None,
) -> bool:
    """True if *now* (or current time) is inside [start, end) in that timezone."""
    if not timezone_name:
        return False
    try:
        tz = _zone(timezone_name)
    except Exception:
        return False
    local = (now or datetime.now(tz)).astimezone(tz)
    if weekdays_only and local.weekday() >= 5:
        return False
    start_t = _parse_hhmm(start, time(10, 0))
    end_t = _parse_hhmm(end, time(14, 0))
    t = local.time()
    if start_t <= end_t:
        return start_t <= t < end_t
    # Overnight window (rare): e.g. 22:00–06:00
    return t >= start_t or t < end_t


def bucket_label(timezone_name: str | None) -> str:
    return timezone_name or "unknown"
