import pytest

from cogs.eventhub import _event_role_name


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
