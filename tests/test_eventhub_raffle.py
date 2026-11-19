from datetime import datetime, timezone
import asyncio
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import discord
from PIL import Image

import cogs.eventhub as eventhub
import cogs.dashboard as dashboard
from cogs.dashboard import EventDashboardView, RaffleInitialEntrantsView, _get_raffle_ready_events
from cogs.eventhub import (
    EventHub,
    _fetch_drand_beacon,
    _raffle_event_is_ready,
    _raffle_entrants_hash,
    _raffle_wheel_animation,
    _raffle_wheel_image,
    _raffle_target_round,
    _select_raffle_winners,
    _can_spin_raffle,
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
    assert "registration_open = FALSE" in migration
    assert "create_event_raffle_draft" in migration
    assert "set_event_raffle_winner_count" in migration
    assert "winner_count BETWEEN 1 AND 10" in migration
    assert "CHECK (winner_count BETWEEN 1 AND 10 OR status <> 'OPEN') NOT VALID" in migration
    assert "p_winner_count NOT BETWEEN 1 AND 10" in migration
    assert "v_raffle.winner_count NOT BETWEEN 1 AND 10" in migration
    assert "'already_present', TRUE" in migration
    assert "Could not add member to this raffle" in migration
    assert "set_event_raffle_channel" in migration
    assert "SET channel_id = p_channel_id," in migration
    assert "created_by = p_actor_id" in migration
    assert "set_event_raffle_spin_message" in migration
    assert "verify_event_raffle_claim" in migration
    assert "verify_event_raffle_claim_code" in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_raffle_claims_code" in migration
    assert "'already_redeemed'" in migration
    assert "'event_created_by', v_event.created_by" in migration
    assert "verification_method', 'CODE_ONLY'" in migration
    assert "TO service_role" in migration
    assert "NOTIFY pgrst, 'reload schema';" in migration
    assert "char_length(p_signature) NOT BETWEEN 64 AND 512" in migration
    assert "p_signature !~ '^[0-9A-Fa-f]+$'" in migration
    assert "{64,512}" not in migration


def test_raffle_slash_commands_are_registered_under_event_group() -> None:
    raffle_group = next(
        command for command in EventHub.__cog_app_commands__
        if command.name == "raffle"
    )

    assert {command.name for command in raffle_group.commands} == {
        "create",
        "preview",
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


def test_raffle_wheel_rotation_changes_the_rendered_orientation() -> None:
    initial = _raffle_wheel_image(ENTRANTS, rotation_degrees=0).getvalue()
    rotated = _raffle_wheel_image(ENTRANTS, rotation_degrees=90).getvalue()

    assert initial != rotated


def test_raffle_wheel_animation_is_smooth_bounded_and_stops_on_winner() -> None:
    animation = _raffle_wheel_animation(ENTRANTS, pointer_index=2)
    gif = Image.open(animation)
    durations = []
    for frame_index in range(gif.n_frames):
        gif.seek(frame_index)
        durations.append(gif.info["duration"])
    sample_point = (
        gif.width // 2,
        gif.height // 2 - round(300 * gif.width / 900),
    )
    gif.seek(0)
    first_frame = gif.convert("RGB").getpixel(sample_point)
    gif.seek(gif.n_frames - 1)
    final_frame = gif.convert("RGB")
    expected_color = tuple(bytes.fromhex(eventhub.WHEEL_COLORS[2][1:]))
    final_color = final_frame.getpixel(sample_point)

    assert gif.format == "GIF"
    assert gif.n_frames == 90
    assert sum(durations) == 4500
    assert gif.info.get("loop") is None
    assert max(abs(actual - expected) for actual, expected in zip(final_color, expected_color)) <= 6
    assert first_frame != final_color


def test_raffle_wheel_stops_selected_entrant_under_fixed_pointer() -> None:
    image = Image.open(_raffle_wheel_image(ENTRANTS, pointer_index=2))
    expected_color = tuple(bytes.fromhex(eventhub.WHEEL_COLORS[2][1:]))

    assert image.getpixel((450, 150)) == expected_color


def test_raffle_wheel_rejects_an_empty_entrant_list() -> None:
    with pytest.raises(ValueError):
        _raffle_wheel_image([])


def test_raffle_spin_is_limited_to_owner_admin_or_setup_staff(monkeypatch) -> None:
    owner_id = 1
    admin_id = 3
    creator_id = 2
    other_staff_id = 4
    monkeypatch.setattr(eventhub, "is_admin", lambda user: user.id in {owner_id, admin_id})
    monkeypatch.setattr(eventhub, "is_staff", lambda user: user.id in {creator_id, other_staff_id})
    guild = SimpleNamespace(owner_id=owner_id)
    raffle = {"created_by": creator_id}

    def interaction_for(user_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=user_id),
        )

    assert _can_spin_raffle(interaction_for(owner_id), raffle)
    assert _can_spin_raffle(interaction_for(admin_id), raffle)
    assert _can_spin_raffle(interaction_for(creator_id), raffle)
    assert not _can_spin_raffle(interaction_for(other_staff_id), raffle)


def test_raffle_wheel_preview_command_is_staff_only(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.sent = None

        async def send_message(self, *args, **kwargs):
            self.sent = (args, kwargs)

    raffle_group = next(
        command for command in EventHub.__cog_app_commands__
        if command.name == "raffle"
    )
    preview_command = next(command for command in raffle_group.commands if command.name == "preview")
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
    )
    monkeypatch.setattr(eventhub, "is_staff", lambda _user: False)

    asyncio.run(preview_command.callback(EventHub(SimpleNamespace()), interaction))

    assert interaction.response.sent[1]["ephemeral"] is True
    assert "للمشرفين فقط" in interaction.response.sent[0][0]


def test_real_raffle_spin_uploads_one_gif_and_waits_for_its_duration(monkeypatch) -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    sleep_calls = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        eventhub,
        "_raffle_wheel_animation",
        lambda _entrants, _winner_index: io.BytesIO(b"gif"),
    )
    monkeypatch.setattr(eventhub.asyncio, "sleep", fake_sleep)
    message = FakeMessage()
    cog = EventHub(SimpleNamespace())

    asyncio.run(
        cog._animate_raffle_wheel_message(
            message,
            {"id": 5, "title": "Preview"},
            ENTRANTS,
            winner_index=1,
        )
    )

    assert len(message.edits) == 1
    assert message.edits[0]["attachments"][0].filename == "raffle-wheel.gif"
    assert message.edits[0]["view"].children[0].disabled
    assert sleep_calls == [4.5]


def test_locking_publishes_only_one_wheel_message(monkeypatch) -> None:
    class FakeMessage:
        id = 900

    class FakeChannel:
        def __init__(self) -> None:
            self.sent_messages: list[dict] = []
            self.mention = "#raffle"

        async def send(self, **kwargs):
            self.sent_messages.append(kwargs)
            return FakeMessage()

    class FakeDatabase:
        def __init__(self, raffle: dict) -> None:
            self.raffle = raffle
            self.spin_message_id = None
            self.lock_message_id = None

        async def get_event_raffle(self, *_args):
            return self.raffle

        async def lock_event_raffle(self, *_args):
            self.raffle["status"] = "LOCKED"
            return self.raffle

        async def prepare_event_raffle_beacon(self, **kwargs):
            self.raffle.update({
                "entrants_hash": kwargs["entrants_hash"],
                "beacon_chain_hash": kwargs["chain_hash"],
                "beacon_target_round": kwargs["target_round"],
            })
            return self.raffle

        async def set_event_raffle_spin_message(self, _raffle_id, _guild_id, message_id):
            self.spin_message_id = message_id
            return True

        async def set_event_raffle_lock_message(self, _raffle_id, _guild_id, message_id):
            self.lock_message_id = message_id
            return True

    class FakeFollowup:
        async def send(self, *_args, **_kwargs):
            return None

    raffle = {
        "id": 2,
        "guild_id": 1,
        "event_id": 22,
        "title": "Test raffle",
        "channel_id": 10,
        "created_by": 20,
        "winner_count": 1,
        "status": "OPEN",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "entrants_snapshot": ENTRANTS,
        "entrants_hash": None,
        "lock_message_id": None,
        "spin_message_id": None,
    }
    channel = FakeChannel()
    database = FakeDatabase(raffle)
    bot = SimpleNamespace(
        db=database,
        add_view=lambda *_args, **_kwargs: None,
        get_channel=lambda _channel_id: channel,
    )
    cog = EventHub(bot)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=20),
        followup=FakeFollowup(),
    )

    async def fetch_drand_info() -> dict:
        return {
            "period": 30,
            "genesis_time": int(datetime.now(timezone.utc).timestamp()) - 60,
            "hash": "a" * 64,
        }

    monkeypatch.setattr(eventhub, "_fetch_drand_info", fetch_drand_info)
    monkeypatch.setattr(eventhub.discord, "TextChannel", FakeChannel)

    asyncio.run(cog.lock_and_publish_raffle(interaction, raffle_id=2))

    assert len(channel.sent_messages) == 1
    assert "embed" in channel.sent_messages[0]
    assert "file" in channel.sent_messages[0]
    assert "files" not in channel.sent_messages[0]
    embed = channel.sent_messages[0]["embed"]
    assert not embed.fields
    assert "drand" not in embed.description.lower()
    assert "بصمة" not in embed.description
    assert "إثبات" not in embed.description
    assert database.spin_message_id == FakeMessage.id
    assert database.lock_message_id == FakeMessage.id


def test_raffle_result_updates_the_wheel_message_without_posting_another(monkeypatch) -> None:
    class FakeWheelMessage:
        id = 900
        attachments = [SimpleNamespace(filename="raffle-wheel.gif")]

        async def edit(self, **kwargs):
            self.edited = kwargs

    class FakeChannel:
        def __init__(self) -> None:
            self.wheel_message = FakeWheelMessage()
            self.mention = "#raffle"

        async def fetch_message(self, message_id: int):
            assert message_id == self.wheel_message.id
            return self.wheel_message

        async def send(self, **_kwargs):
            raise AssertionError("The result must update the existing wheel message.")

    class FakeDatabase:
        async def set_event_raffle_result_message(self, _raffle_id, _guild_id, message_id):
            self.result_message_id = message_id
            return True

    class FakeFollowup:
        async def send(self, *_args, **_kwargs):
            return None

    channel = FakeChannel()
    bot = SimpleNamespace(
        db=FakeDatabase(),
        get_channel=lambda _channel_id: channel,
    )
    cog = EventHub(bot)
    interaction = SimpleNamespace(followup=FakeFollowup())
    raffle = {
        "id": 2,
        "guild_id": 1,
        "channel_id": 10,
        "spin_message_id": FakeWheelMessage.id,
        "title": "Test raffle",
        "winner_ids": [100],
        "entrants_snapshot": ENTRANTS,
        "entrants_hash": _raffle_entrants_hash(ENTRANTS),
        "beacon_target_round": 42,
        "result_message_id": None,
    }

    monkeypatch.setattr(eventhub.discord, "TextChannel", FakeChannel)

    asyncio.run(cog._publish_raffle_result(interaction, raffle))

    assert "embed" in channel.wheel_message.edited
    embed = channel.wheel_message.edited["embed"]
    assert not embed.fields
    assert "drand" not in embed.description.lower()
    assert raffle["entrants_hash"] not in embed.description
    assert embed.image.url == "attachment://raffle-wheel.gif"
    assert channel.wheel_message.edited["view"].children[0].disabled
    assert bot.db.result_message_id == FakeWheelMessage.id


def test_existing_wheel_proof_is_removed_from_the_public_embed() -> None:
    class FakeWheelMessage:
        def __init__(self) -> None:
            self.attachments = []
            self.embeds = [discord.Embed(description="old public proof")]
            self.embeds[0].add_field(name="إثبات السحب", value="drand round 42")

        async def edit(self, **kwargs):
            self.edited = kwargs

    cog = EventHub(SimpleNamespace())
    message = FakeWheelMessage()
    raffle = {
        "id": 2,
        "title": "Test raffle",
        "status": "LOCKED",
        "winner_count": 1,
        "entrants_snapshot": ENTRANTS,
        "winner_ids": [],
    }

    asyncio.run(cog._refresh_raffle_wheel_without_proof(message, raffle))

    embed = message.edited["embed"]
    assert not embed.fields
    assert "drand" not in embed.description.lower()
    assert "إثبات" not in embed.description


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"status": "OPEN", "registration_open": True}, False),
        ({"status": "OPEN", "registration_open": False}, True),
        ({"status": "CLOSED", "registration_open": True}, True),
        ({"status": "CANCELLED", "registration_open": False}, False),
    ],
)
def test_raffle_accepts_only_ended_or_registration_closed_events(
    event: dict,
    expected: bool,
) -> None:
    assert _raffle_event_is_ready(event) is expected


def test_dashboard_raffle_event_picker_includes_registration_closed_events() -> None:
    events = [
        {"id": 18, "guild_id": 1, "status": "OPEN", "registration_open": False, "ends_at": "2026-11-20"},
        {"id": 19, "guild_id": 1, "status": "OPEN", "registration_open": True, "ends_at": "2026-11-21"},
        {"id": 20, "guild_id": 1, "status": "CLOSED", "registration_open": True, "ends_at": "2026-11-18"},
        {"id": 21, "guild_id": 1, "status": "CANCELLED", "registration_open": False, "ends_at": "2026-11-17"},
    ]

    class EventQuery:
        def __init__(self) -> None:
            self.filters: dict[str, object] = {}
            self.limit_value = 25

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, column: str, value: object):
            self.filters[column] = value
            return self

        def order(self, *_args, **_kwargs):
            return self

        def limit(self, value: int):
            self.limit_value = value
            return self

        async def execute(self):
            result = [
                event for event in events
                if all(event.get(column) == value for column, value in self.filters.items())
            ]
            return SimpleNamespace(data=result[:self.limit_value])

    class Client:
        def table(self, name: str):
            assert name == "events"
            return EventQuery()

    ready = asyncio.run(_get_raffle_ready_events(Client(), guild_id=1))

    assert {event["id"] for event in ready} == {18, 20}


def test_event_dashboard_buttons_are_grouped_by_workflow() -> None:
    view = EventDashboardView(None)
    buttons_by_row = {
        row: [item.label for item in view.children if item.row == row]
        for row in range(3)
    }

    assert buttons_by_row == {
        0: ["إنشاء فعالية", "قائمة الفعاليات", "إغلاق التسجيل"],
        1: ["تعديل فعالية", "إلغاء فعالية", "تسجيل الحضور", "الرئيسية"],
        2: ["إعداد السحب", "تحقق من رمز الفائز"],
    }


def test_admin_can_verify_claim_code_without_raffle_or_member_ids(monkeypatch) -> None:
    token = "winner-code"
    expected_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    class FakeDatabase:
        call = None

        async def verify_event_raffle_claim_code(self, **kwargs):
            self.call = kwargs
            return {
                "result": "verified",
                "user_id": 123,
                "raffle_id": 45,
                "raffle_title": "السحب التجريبي",
                "raffle_created_by": 99,
                "drawn_at": "2026-04-02T10:00:00+00:00",
                "redeemed_at": "2026-04-03T11:00:00+00:00",
                "event_id": 18,
                "event_title": "الفعالية التجريبية",
                "event_created_by": 77,
                "event_starts_at": "2026-04-01T10:00:00+00:00",
            }

    class FakeResponse:
        async def defer(self, **_kwargs):
            pass

        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("Authorized verification should not be rejected")

    class FakeFollowup:
        sent = None

        async def send(self, **kwargs):
            self.sent = kwargs

    database = FakeDatabase()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )
    modal = dashboard.VerifyRaffleClaimModal(SimpleNamespace(bot=SimpleNamespace(db=database)))
    modal.claim_code._value = token
    monkeypatch.setattr(dashboard, "_admin", lambda _interaction: True)

    asyncio.run(modal.on_submit(interaction))

    assert database.call == {
        "guild_id": 1,
        "actor_id": 2,
        "token_hash": expected_hash,
    }
    embed = interaction.followup.sent["embed"]
    rendered_fields = {field.name: field.value for field in embed.fields}
    assert "<@123>" in rendered_fields["الفائز"]
    assert "الفعالية التجريبية" in rendered_fields["الفعالية"]
    assert "<@77>" in rendered_fields["منشئ الفعالية"]
    assert rendered_fields["وقت السحب"] == "<t:1775124000:F>"
    assert "تم التحقق من الرمز" in embed.title


def test_invalid_claim_code_does_not_show_claim_details(monkeypatch) -> None:
    class FakeDatabase:
        async def verify_event_raffle_claim_code(self, **_kwargs):
            return {"result": "invalid"}

    class FakeResponse:
        async def defer(self, **_kwargs):
            pass

        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("Authorized verification should be deferred")

    class FakeFollowup:
        sent = None

        async def send(self, *args, **kwargs):
            self.sent = (args, kwargs)

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )
    modal = dashboard.VerifyRaffleClaimModal(SimpleNamespace(bot=SimpleNamespace(db=FakeDatabase())))
    modal.claim_code._value = "wrong-code"
    monkeypatch.setattr(dashboard, "_admin", lambda _interaction: True)

    asyncio.run(modal.on_submit(interaction))

    assert "غير صحيح" in interaction.followup.sent[0][0]
    assert "embed" not in interaction.followup.sent[1]


def test_redeemed_claim_code_reports_reuse_and_keeps_claim_identity(monkeypatch) -> None:
    class FakeDatabase:
        async def verify_event_raffle_claim_code(self, **_kwargs):
            return {
                "result": "already_redeemed",
                "user_id": 123,
                "raffle_id": 45,
                "raffle_title": "السحب التجريبي",
                "drawn_at": "2026-04-02T10:00:00+00:00",
                "redeemed_at": "2026-04-03T11:00:00+00:00",
            }

    class FakeResponse:
        async def defer(self, **_kwargs):
            pass

    class FakeFollowup:
        sent = None

        async def send(self, **kwargs):
            self.sent = kwargs

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )
    modal = dashboard.VerifyRaffleClaimModal(SimpleNamespace(bot=SimpleNamespace(db=FakeDatabase())))
    modal.claim_code._value = "redeemed-code"
    monkeypatch.setattr(dashboard, "_admin", lambda _interaction: True)

    asyncio.run(modal.on_submit(interaction))

    embed = interaction.followup.sent["embed"]
    assert "استُخدم سابقًا" in embed.title
    assert "<@123>" in next(field.value for field in embed.fields if field.name == "الفائز")


def test_claim_code_modal_rejects_non_admin(monkeypatch) -> None:
    class FakeDatabase:
        async def verify_event_raffle_claim_code(self, **_kwargs):
            raise AssertionError("Non-admins must not verify claim codes")

    class FakeResponse:
        sent = None

        async def send_message(self, *args, **kwargs):
            self.sent = (args, kwargs)

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
    )
    modal = dashboard.VerifyRaffleClaimModal(SimpleNamespace(bot=SimpleNamespace(db=FakeDatabase())))
    modal.claim_code._value = "winner-code"
    monkeypatch.setattr(dashboard, "_admin", lambda _interaction: False)

    asyncio.run(modal.on_submit(interaction))

    assert "Administrator" in interaction.response.sent[0][0]


def test_raffle_setup_collects_entrants_before_channel_selection() -> None:
    view = RaffleInitialEntrantsView(None, event_id=18)
    winner_count_select = next(
        item for item in view.children
        if isinstance(item, discord.ui.Select)
    )

    assert any(item.label == "متابعة" for item in view.children)
    assert any(isinstance(item, discord.ui.UserSelect) for item in view.children)
    assert not any(isinstance(item, discord.ui.ChannelSelect) for item in view.children)
    assert [option.value for option in winner_count_select.options] == [
        str(count) for count in range(1, 11)
    ]
    assert [option.value for option in winner_count_select.options if option.default] == ["1"]


def test_raffle_winner_count_can_only_be_changed_by_admin(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.sent = None

        async def send_message(self, *args, **kwargs):
            self.sent = (args, kwargs)

    monkeypatch.setattr(dashboard, "_admin", lambda _interaction: False)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=FakeResponse(),
    )

    asyncio.run(
        RaffleInitialEntrantsView(None, event_id=18).select_winner_count(interaction)
    )

    assert "Administrator" in interaction.response.sent[0][0]


def test_admin_selected_winner_count_is_saved_with_dashboard_draft(monkeypatch) -> None:
    class FakeMember:
        id = 5
        display_name = "Eligible"
        bot = False

    class FakeDatabase:
        def __init__(self) -> None:
            self.draft_kwargs = None

        async def create_event_raffle_draft(self, **kwargs):
            self.draft_kwargs = kwargs
            return {"id": 77}

        async def manage_event_raffle_entry(self, **_kwargs):
            return {"already_present": False}

    class FakeResponse:
        async def edit_message(self, **_kwargs):
            return None

        async def defer(self, **_kwargs):
            return None

    class FakeFollowup:
        async def send(self, *_args, **_kwargs):
            return None

    async def run_test() -> None:
        monkeypatch.setattr(dashboard, "_admin", lambda _interaction: True)
        monkeypatch.setattr(dashboard, "_staff", lambda _interaction: True)
        monkeypatch.setattr(dashboard.discord, "Member", FakeMember)
        database = FakeDatabase()
        cog = SimpleNamespace(bot=SimpleNamespace(db=database))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=2),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        view = RaffleInitialEntrantsView(cog, event_id=18)
        view.winner_count_select._values = ["3"]
        await view.select_winner_count(interaction)
        view.select = SimpleNamespace(values=[FakeMember()])

        continue_button = next(
            item for item in view.children if item.label == "متابعة"
        )
        await continue_button.callback(interaction)

        assert database.draft_kwargs["winner_count"] == 3

    asyncio.run(run_test())


def test_admin_can_change_winner_count_on_an_open_dashboard_draft(monkeypatch) -> None:
    class FakeMember:
        id = 5
        display_name = "Eligible"
        bot = False

    class FakeDatabase:
        def __init__(self) -> None:
            self.updated_winner_count = None

        async def get_event_raffle(self, *_args):
            return {
                "id": 77,
                "event_id": 18,
                "status": "OPEN",
                "winner_count": 1,
            }

        async def set_event_raffle_winner_count(self, **kwargs):
            self.updated_winner_count = kwargs["winner_count"]

        async def manage_event_raffle_entry(self, **_kwargs):
            return {"already_present": False}

    class FakeResponse:
        async def edit_message(self, **_kwargs):
            return None

        async def defer(self, **_kwargs):
            return None

    class FakeFollowup:
        async def send(self, *_args, **_kwargs):
            return None

    async def run_test() -> None:
        monkeypatch.setattr(dashboard, "_admin", lambda _interaction: True)
        monkeypatch.setattr(dashboard, "_staff", lambda _interaction: True)
        monkeypatch.setattr(dashboard.discord, "Member", FakeMember)
        database = FakeDatabase()
        cog = SimpleNamespace(bot=SimpleNamespace(db=database))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=2),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        view = RaffleInitialEntrantsView(
            cog,
            event_id=18,
            raffle_id=77,
        )
        view.winner_count_select._values = ["4"]
        await view.select_winner_count(interaction)
        view.select = SimpleNamespace(values=[FakeMember()])

        continue_button = next(
            item for item in view.children if item.label == "متابعة"
        )
        await continue_button.callback(interaction)

        assert database.updated_winner_count == 4


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


def test_drand_fetch_uses_another_mirror_when_primary_fails(monkeypatch) -> None:
    requested_urls: list[str] = []

    class FakeResponse:
        def __init__(self, url: str) -> None:
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            if self.url.startswith("https://primary.example"):
                raise eventhub.aiohttp.ClientError("primary unavailable")

        async def json(self) -> dict:
            return {"hash": "a" * 64}

    class FakeSession:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url: str) -> FakeResponse:
            requested_urls.append(url)
            return FakeResponse(url)

    monkeypatch.setattr(eventhub, "DRAND_API_BASES", (
        "https://primary.example",
        "https://mirror.example",
    ))
    monkeypatch.setattr(eventhub.aiohttp, "ClientSession", FakeSession)

    result = asyncio.run(eventhub._fetch_drand_json("/info"))

    assert result == {"hash": "a" * 64}
    assert requested_urls == [
        "https://primary.example/info",
        "https://mirror.example/info",
    ]


def test_drand_fetch_reports_status_for_every_unavailable_mirror(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, url: str) -> None:
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            raise eventhub.aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url=self.url),
                history=(),
                status=503,
                message="Service unavailable",
            )

    class FakeSession:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url: str) -> FakeResponse:
            return FakeResponse(url)

    monkeypatch.setattr(eventhub, "DRAND_API_BASES", (
        "https://primary.example",
        "https://mirror.example",
    ))
    monkeypatch.setattr(eventhub.aiohttp, "ClientSession", FakeSession)

    with pytest.raises(eventhub.DrandUnavailableError) as error:
        asyncio.run(eventhub._fetch_drand_json("/info"))

    assert error.value.failures == (
        "primary.example: HTTP 503",
        "mirror.example: HTTP 503",
    )


def test_drand_425_marks_the_pinned_round_as_not_yet_published(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, url: str) -> None:
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            status = 404 if "cloudflare" in self.url else 425
            raise eventhub.aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url=self.url),
                history=(),
                status=status,
                message="Not available yet",
            )

    class FakeSession:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url: str) -> FakeResponse:
            return FakeResponse(url)

    monkeypatch.setattr(eventhub, "DRAND_API_BASES", (
        "https://primary.example",
        "https://api2.example",
        "https://api3.example",
        "https://drand.cloudflare.com",
    ))
    monkeypatch.setattr(eventhub.aiohttp, "ClientSession", FakeSession)

    with pytest.raises(eventhub.DrandRoundNotAvailableError) as error:
        asyncio.run(eventhub._fetch_drand_json("/public/42"))

    assert error.value.path == "/public/42"
    assert error.value.failures == (
        "primary.example: HTTP 425",
        "api2.example: HTTP 425",
        "api3.example: HTTP 425",
        "drand.cloudflare.com: HTTP 404",
    )


def test_spin_reports_when_the_pinned_round_is_not_published(monkeypatch) -> None:
    class FakeDatabase:
        async def get_event_raffle(self, *_args):
            return {
                "id": 42,
                "guild_id": 1,
                "status": "LOCKED",
                "lock_message_id": 100,
                "spin_message_id": 101,
                "beacon_chain_hash": "a" * 64,
                "beacon_target_round": 300,
            }

    class FakeFollowup:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, *args, **kwargs):
            self.messages.append((args, kwargs))

    async def fetch_not_yet_published(_round_number: int) -> dict:
        raise eventhub.DrandRoundNotAvailableError("/public/300", ["api.drand.sh: HTTP 425"])

    monkeypatch.setattr(eventhub, "_can_spin_raffle", lambda *_args: True)
    monkeypatch.setattr(
        EventHub,
        "_refresh_raffle_wheel_without_proof",
        lambda *_args: asyncio.sleep(0),
    )
    monkeypatch.setattr(eventhub, "_fetch_drand_info", lambda: asyncio.sleep(0, result={"hash": "a" * 64}))
    monkeypatch.setattr(eventhub, "_fetch_drand_beacon", fetch_not_yet_published)

    followup = FakeFollowup()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(is_done=lambda: True),
        followup=followup,
    )
    wheel_message = SimpleNamespace(id=101)
    cog = EventHub(SimpleNamespace(db=FakeDatabase()))

    asyncio.run(cog.spin_raffle(interaction, 42, wheel_message))

    assert "لم تصدر بعد" in followup.messages[0][0][0]
    assert "جولة بديلة" in followup.messages[0][0][0]
