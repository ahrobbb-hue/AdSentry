import pytest

from datetime import datetime, timezone

from cogs.eventhub import _event_end_datetime, _event_role_name, _parse_event_datetime
from cogs.setup_cog import event_text_channel_name


@pytest.mark.parametrize(
    ("slot", "name"),
    [
        (1, "Event"),
        (2, "Event 2"),
        (3, "Event 3"),
        (4, "Event 4"),
        (5, "Event 5"),
    ],
)
def test_event_role_name_matches_slot(slot: int, name: str) -> None:
    assert _event_role_name(slot) == name


@pytest.mark.parametrize("slot", [0, 6])
def test_event_role_name_rejects_slots_outside_the_five_role_pool(slot: int) -> None:
    with pytest.raises(ValueError):
        _event_role_name(slot)


@pytest.mark.parametrize(
    ("slot", "name"),
    [
        (1, "╠〖🎪┇〢event-1"),
        (2, "╠〖🎪┇〢event-2"),
        (3, "╠〖🎪┇〢event-3"),
        (4, "╠〖🎪┇〢event-4"),
        (5, "╠〖🎪┇〢event-5"),
    ],
)
def test_event_text_channel_name_matches_role_slot(slot: int, name: str) -> None:
    assert event_text_channel_name(slot) == name


@pytest.mark.parametrize("slot", [0, 6])
def test_event_text_channel_name_rejects_slots_outside_the_five_role_pool(slot: int) -> None:
    with pytest.raises(ValueError):
        event_text_channel_name(slot)


def test_event_datetime_parser_normalizes_timezone_to_utc() -> None:
    parsed = _parse_event_datetime("2027-01-15T20:30:00+03:00")

    assert parsed == datetime(2027, 1, 15, 17, 30, tzinfo=timezone.utc)


def test_event_end_datetime_uses_configured_duration() -> None:
    start = datetime(2027, 1, 15, 17, 30, tzinfo=timezone.utc)

    assert _event_end_datetime(start, 3) == datetime(2027, 1, 15, 20, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("duration_hours", [0, 169])
def test_event_end_datetime_rejects_duration_outside_one_week(duration_hours: int) -> None:
    start = datetime(2027, 1, 15, 17, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        _event_end_datetime(start, duration_hours)


@pytest.mark.parametrize(
    "value",
    ["2027-01-15T20:30:00", "not-a-date", "2020-01-01T20:30:00+03:00"],
)
def test_event_datetime_parser_rejects_missing_timezone_invalid_and_past_times(value: str) -> None:
    with pytest.raises(ValueError):
        _parse_event_datetime(value)
