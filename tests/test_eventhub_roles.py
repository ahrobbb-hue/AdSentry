import pytest

from datetime import datetime, timedelta, timezone

from cogs.eventhub import (
    RegisterView,
    _event_end_datetime,
    _event_embed,
    _parse_event_duration,
    _event_role_name,
    _parse_event_datetime,
)
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

    assert _event_end_datetime(start, _parse_event_duration("30m")) == datetime(2027, 1, 15, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected_minutes"),
    [("30m", 30), ("1h", 60), ("1d", 1440), ("2w", 20160), ("2", 120)],
)
def test_event_duration_parser_supports_units_and_legacy_hours(value: str, expected_minutes: int) -> None:
    assert _parse_event_duration(value).total_seconds() == expected_minutes * 60


def test_closed_registration_disables_signup_but_keeps_cancel_registration() -> None:
    view = RegisterView(42, registration_open=False)

    assert view.children[0].disabled
    assert not view.children[1].disabled


def test_event_embed_marks_registration_closed_without_closing_event() -> None:
    embed = _event_embed({
        "id": 42,
        "title": "Test",
        "description": "Test event",
        "starts_at": datetime(2027, 1, 15, 17, 30, tzinfo=timezone.utc),
        "ends_at": datetime(2027, 1, 15, 20, 30, tzinfo=timezone.utc),
        "status": "OPEN",
        "registration_open": False,
        "capacity": 10,
    })

    registration_field = next(field for field in embed.fields if field.name == "📝  التسجيل")
    assert "مغلق" in registration_field.value
    assert "الفعالية مستمرة" in registration_field.value


@pytest.mark.parametrize("value", ["0m", "5w", "tomorrow", ""])
def test_event_duration_parser_rejects_invalid_or_overlong_values(value: str) -> None:
    with pytest.raises(ValueError):
        _parse_event_duration(value)


@pytest.mark.parametrize("duration", [timedelta(seconds=30), timedelta(weeks=4, minutes=1)])
def test_event_end_datetime_rejects_duration_outside_supported_range(duration: timedelta) -> None:
    start = datetime(2027, 1, 15, 17, 30, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        _event_end_datetime(start, duration)


@pytest.mark.parametrize(
    "value",
    ["2027-01-15T20:30:00", "not-a-date", "2020-01-01T20:30:00+03:00"],
)
def test_event_datetime_parser_rejects_missing_timezone_invalid_and_past_times(value: str) -> None:
    with pytest.raises(ValueError):
        _parse_event_datetime(value)
