from datetime import datetime, timezone
import asyncio
import hashlib
from pathlib import Path

import pytest

import cogs.eventhub as eventhub
from cogs.eventhub import (
    EventHub,
    _fetch_drand_beacon,
    _raffle_entrants_hash,
    _raffle_wheel_image,
    _raffle_target_round,
    _select_raffle_winners,
)


ENTRANTS = [
    {"user_id": 300, "user_name": "C"},
    {"user_id": 100, "user_name": "A"},
    {"user_id": 200, "user_name": "B"},
]


def test_entrant_hash_is_stable_and_order_independent() -> None:
    assert _raffle_entrants_hash(ENTRANTS) == _raffle_entrants_hash(list(reversed(ENTRANTS)))


def test_raffle_winners_are_reproducible_and_unique() -> None:
    entrants_hash = _raffle_entrants_hash(ENTRANTS)

    winners = _select_raffle_winners(
        randomness="a" * 64,
        raffle_id=42,
        entrants=ENTRANTS,
        winner_count=2,
        entrants_hash=entrants_hash,
    )
    repeated = _select_raffle_winners(
        randomness="a" * 64,
        raffle_id=42,
        entrants=list(reversed(ENTRANTS)),
        winner_count=2,
        entrants_hash=entrants_hash,
    )

    assert winners == repeated
    assert len(set(winners)) == 2
    assert set(winners).issubset({100, 200, 300})


def test_target_round_is_safely_after_lock_time() -> None:
    locked_at = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    target_round = _raffle_target_round(
        locked_at.isoformat(),
        {"period": 30, "genesis_time": int(locked_at.timestamp()) - 60},
    )

    assert target_round == 6


@pytest.mark.parametrize(
    ("randomness", "winner_count"),
    [
        ("bad", 1),
        ("a" * 64, 0),
        ("a" * 64, 4),
    ],
)
def test_raffle_winner_selection_rejects_invalid_input(
    randomness: str,
    winner_count: int,
) -> None:
    with pytest.raises(ValueError):
        _select_raffle_winners(randomness, 42, ENTRANTS, winner_count)


def test_raffle_migration_freezes_entries_and_restricts_draw_rpc() -> None:
    migration = (
        Path(__file__).parent.parent
        / "database"
        / "eventhub_raffle_migration.sql"
    ).read_text(encoding="utf-8")

    assert "guard_event_raffle_entry" in migration
    assert "lock_event_raffle" in migration
    assert "record_event_raffle_result" in migration
    assert "status = 'CLOSED'" in migration
    assert "set_event_raffle_spin_message" in migration
    assert "verify_event_raffle_claim" in migration
    assert "TO service_role" in migration


def test_raffle_slash_commands_are_registered_under_event_group() -> None:
    raffle_group = next(
        command for command in EventHub.__cog_app_commands__
        if command.name == "raffle"
    )

    assert {command.name for command in raffle_group.commands} == {
        "create",
        "add",
        "remove",
        "list",
        "lock",
        "draw",
        "verify",
        "resend-code",
        "verify-claim",
        "cancel",
    }


def test_raffle_wheel_renders_a_png_for_the_locked_entrants() -> None:
    image = _raffle_wheel_image(ENTRANTS, pointer_index=2)

    assert image.read(8) == b"\x89PNG\r\n\x1a\n"


def test_raffle_wheel_rejects_an_empty_entrant_list() -> None:
    with pytest.raises(ValueError):
        _raffle_wheel_image([])


def test_drand_beacon_requires_randomness_to_match_signature(monkeypatch) -> None:
    signature = "ab" * 96
    randomness = hashlib.sha256(bytes.fromhex(signature)).hexdigest()

    async def fetch_json(path: str) -> dict:
        assert path == "/public/12"
        return {"round": 12, "randomness": randomness, "signature": signature}

    monkeypatch.setattr(eventhub, "_fetch_drand_json", fetch_json)

    assert asyncio.run(_fetch_drand_beacon(12))["round"] == 12


def test_drand_beacon_rejects_mismatched_signature(monkeypatch) -> None:
    async def fetch_json(path: str) -> dict:
        return {"round": 12, "randomness": "0" * 64, "signature": "ab" * 96}

    monkeypatch.setattr(eventhub, "_fetch_drand_json", fetch_json)

    with pytest.raises(ValueError):
        asyncio.run(_fetch_drand_beacon(12))
