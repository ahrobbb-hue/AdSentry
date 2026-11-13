from datetime import datetime, timezone

import pytest

from cogs.recruitment import _parse_recruitment_deadline


def test_parse_recruitment_deadline_normalizes_timezone_to_utc():
    parsed = _parse_recruitment_deadline("2099-01-15T20:30:00+03:00")

    assert parsed == datetime(2099, 1, 15, 17, 30, tzinfo=timezone.utc)


def test_parse_recruitment_deadline_allows_manual_close():
    assert _parse_recruitment_deadline("") is None


@pytest.mark.parametrize(
    "value",
    [
        "2099-01-15T20:30:00",
        "not-a-date",
        "2000-01-15T20:30:00+03:00",
    ],
)
def test_parse_recruitment_deadline_rejects_naive_invalid_or_past_values(value):
    with pytest.raises(ValueError):
        _parse_recruitment_deadline(value)
