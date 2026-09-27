"""Location -> timezone resolution for "what time is it in <place>?", using the stdlib `zoneinfo` database only
(no new dependency, no network call, no LLM guess): the current time for a location always comes from
`datetime.now(ZoneInfo(...))`, never from a language model's static knowledge (which can be stale or simply wrong).

This is a lookup table of real-world geography, not a natural-language phrase list -- every timezone-aware system
needs one (the IANA database itself is exactly this: place names to timezone rules). The natural-language side (which
phrasings mean "what time is it") is handled by the regex in `agent/intelligence/router.py`; this module only turns an
already-extracted location word into a `ZoneInfo`, or says honestly that it doesn't know one.

Countries that do not have one national time (Canada, the US, Australia, Russia, Brazil, ...) are never resolved to an
arbitrary city's zone: `resolve_location_timezone` reports them as ambiguous with the major zones to choose from,
rather than silently picking one and being wrong for most of the country.
"""

import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class TimeZoneLookup:
    zone: ZoneInfo | None = None
    ambiguous: bool = False               # a real multi-timezone country/region; `suggestions` names the choices
    suggestions: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.zone is not None or self.ambiguous


# Countries/regions with no single national time. Listed cities are illustrative (a few major ones), not exhaustive.
_AMBIGUOUS: dict[str, list[str]] = {
    "canada": ["Toronto", "Vancouver", "Calgary", "Montreal", "Halifax"],
    "united states": ["New York", "Chicago", "Denver", "Los Angeles", "Anchorage", "Honolulu"],
    "usa": ["New York", "Chicago", "Denver", "Los Angeles", "Anchorage", "Honolulu"],
    "us": ["New York", "Chicago", "Denver", "Los Angeles", "Anchorage", "Honolulu"],
    "the united states": ["New York", "Chicago", "Denver", "Los Angeles", "Anchorage", "Honolulu"],
    "america": ["New York", "Chicago", "Denver", "Los Angeles", "Anchorage", "Honolulu"],
    "australia": ["Sydney", "Perth", "Adelaide", "Brisbane", "Darwin"],
    "russia": ["Moscow", "Yekaterinburg", "Novosibirsk", "Vladivostok"],
    "brazil": ["Sao Paulo", "Manaus", "Fernando de Noronha"],
    "mexico": ["Mexico City", "Tijuana", "Cancun"],
    "indonesia": ["Jakarta", "Makassar", "Jayapura"],
    "kazakhstan": ["Almaty", "Aktobe"],
    "mongolia": ["Ulaanbaatar", "Hovd"],
    "democratic republic of the congo": ["Kinshasa", "Lubumbashi"],
    "chile": ["Santiago", "Easter Island"],
    "ecuador": ["Quito", "Galapagos"],
    "portugal": ["Lisbon", "Ponta Delgada (Azores)"],
    "spain": ["Madrid", "Las Palmas (Canary Islands)"],
    "france": ["Paris", "Cayenne (French Guiana)", "Papeete (Tahiti)"],
    "united kingdom overseas territories": [],  # placeholder to keep this table's shape obvious for future additions
    "antarctica": ["McMurdo Station", "Rothera Station"],
}

# A curated, real IANA timezone for single-timezone countries and major world cities. Keys are normalized (lowercase,
# no punctuation) location words as they'd appear after the intelligence router strips a "what time is it in ..." stem.
_ZONES: dict[str, str] = {
    # major cities named in the product spec / common requests
    "london": "Europe/London", "tokyo": "Asia/Tokyo", "new york": "America/New_York", "new york city": "America/New_York",
    "bengaluru": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "chennai": "Asia/Kolkata", "hyderabad": "Asia/Kolkata", "pune": "Asia/Kolkata",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin", "madrid": "Europe/Madrid", "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
    "dublin": "Europe/Dublin", "lisbon": "Europe/Lisbon", "zurich": "Europe/Zurich", "vienna": "Europe/Vienna", "athens": "Europe/Athens",
    "moscow": "Europe/Moscow", "istanbul": "Europe/Istanbul", "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "seoul": "Asia/Seoul", "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta", "manila": "Asia/Manila", "kuala lumpur": "Asia/Kuala_Lumpur",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "perth": "Australia/Perth", "auckland": "Pacific/Auckland",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver", "montreal": "America/Toronto", "calgary": "America/Edmonton",
    "chicago": "America/Chicago", "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "denver": "America/Denver", "houston": "America/Chicago", "miami": "America/New_York", "boston": "America/New_York",
    "mexico city": "America/Mexico_City", "sao paulo": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires", "lagos": "Africa/Lagos", "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi",
    "johannesburg": "Africa/Johannesburg", "cape town": "Africa/Johannesburg", "tel aviv": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem",
    # single-timezone countries (safe to resolve directly; multi-timezone countries are in _AMBIGUOUS instead)
    "japan": "Asia/Tokyo", "uk": "Europe/London", "united kingdom": "Europe/London", "britain": "Europe/London", "england": "Europe/London",
    "ireland": "Europe/Dublin", "germany": "Europe/Berlin", "italy": "Europe/Rome", "netherlands": "Europe/Amsterdam",
    "switzerland": "Europe/Zurich", "austria": "Europe/Vienna", "greece": "Europe/Athens", "turkey": "Europe/Istanbul",
    "sweden": "Europe/Stockholm", "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen", "finland": "Europe/Helsinki",
    "poland": "Europe/Warsaw", "belgium": "Europe/Brussels", "uae": "Asia/Dubai", "united arab emirates": "Asia/Dubai",
    "india": "Asia/Kolkata", "singapore city": "Asia/Singapore", "south korea": "Asia/Seoul", "korea": "Asia/Seoul",
    "thailand": "Asia/Bangkok", "vietnam": "Asia/Ho_Chi_Minh", "philippines": "Asia/Manila", "malaysia": "Asia/Kuala_Lumpur",
    "china": "Asia/Shanghai", "taiwan": "Asia/Taipei", "new zealand": "Pacific/Auckland", "egypt": "Africa/Cairo",
    "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi", "south africa": "Africa/Johannesburg", "israel": "Asia/Jerusalem",
    "argentina": "America/Argentina/Buenos_Aires", "colombia": "America/Bogota", "peru": "America/Lima",
}


def _normalize(location: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]", "", location.lower()).strip()
    return re.sub(r"^the\s+", "", cleaned)  # "the US" / "the UK" / "the Philippines" all key on the bare name


def resolve_location_timezone(location: str) -> TimeZoneLookup:
    """`location` is the free-text place name already extracted from the user's question (e.g. "canada", "new york",
    "the uk"). Returns a `TimeZoneLookup`: `.zone` set for an unambiguous single-timezone place, `.ambiguous=True`
    with `.suggestions` for a real multi-timezone country, or neither (`.known` False) if the place isn't in this
    table -- in which case the caller must say honestly that it doesn't have that timezone, never guess one."""
    key = _normalize(location)
    if not key:
        return TimeZoneLookup()
    if key in _AMBIGUOUS:
        return TimeZoneLookup(ambiguous=True, suggestions=_AMBIGUOUS[key])
    tz_name = _ZONES.get(key)
    if tz_name is None:
        return TimeZoneLookup()
    try:
        return TimeZoneLookup(zone=ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:  # the platform's tz database is incomplete; honest "don't know" beats a crash
        return TimeZoneLookup()
