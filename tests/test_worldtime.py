"""agent.intelligence.worldtime: real IANA timezone resolution for "what time is it in <place>?" -- geography lookup,
never a language model's guess, and multi-timezone countries are never silently collapsed to one city."""

from zoneinfo import ZoneInfo

import pytest

from agent.intelligence.worldtime import resolve_location_timezone


@pytest.mark.parametrize("location,tz_key", [
    ("london", "Europe/London"), ("Tokyo", "Asia/Tokyo"), ("new york", "America/New_York"),
    ("Bengaluru", "Asia/Kolkata"), ("bangalore", "Asia/Kolkata"), ("Japan", "Asia/Tokyo"),
    ("uk", "Europe/London"), ("united kingdom", "Europe/London"), ("india", "Asia/Kolkata"),
    ("Paris", "Europe/Paris"), ("dubai", "Asia/Dubai"), ("singapore", "Asia/Singapore"),
])
def test_resolves_known_single_timezone_locations(location, tz_key):
    result = resolve_location_timezone(location)
    assert result.known and result.ambiguous is False
    assert result.zone == ZoneInfo(tz_key)


@pytest.mark.parametrize("country", ["canada", "Canada", "united states", "US", "usa", "australia", "russia", "brazil"])
def test_multi_timezone_countries_are_never_silently_resolved_to_one_city(country):
    """A country with no single national time must never be answered as if it had one -- that would be a fabricated
    "current time" for most of the country."""
    result = resolve_location_timezone(country)
    assert result.known is True
    assert result.ambiguous is True and result.zone is None
    assert len(result.suggestions) >= 2  # real, named alternatives, not an empty "figure it out yourself"


def test_unknown_location_is_reported_as_unknown_not_guessed():
    result = resolve_location_timezone("Narnia")
    assert result.known is False and result.zone is None and result.ambiguous is False


def test_case_and_whitespace_insensitive():
    assert resolve_location_timezone("  LONDON  ").zone == ZoneInfo("Europe/London")
    assert resolve_location_timezone("New York").zone == resolve_location_timezone("new york").zone


def test_never_raises_on_odd_input():
    for bad in ("", "   ", "🎉" * 20, "a" * 500, "12345", "!!!"):
        try:
            result = resolve_location_timezone(bad)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"resolve_location_timezone raised on {bad!r}: {exc}")
        assert result.known is False or result.ambiguous or result.zone is not None
