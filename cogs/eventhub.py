"""EventHub: professional events management — UX-optimised for staff and members."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.moderation import is_staff

logger = logging.getLogger("AdSentry.EventHub")

EVENT_ROLE_NAMES = ("Event", "Event 2", "Event 3", "Event 4", "Event 5")
_EVENT_ROLE_LOCKS: dict[int, asyncio.Lock] = {}


def _event_role_name(slot: int) -> str:
    if not 1 <= slot <= len(EVENT_ROLE_NAMES):
        raise ValueError(f"Event role slot must be between 1 and {len(EVENT_ROLE_NAMES)}.")
    return EVENT_ROLE_NAMES[slot - 1]

# ─── Smart Date Parser ────────────────────────────────────────────────────────

def _parse_date(text: str, tz_offset: int = 3) -> datetime:
    """
    Accept human-friendly date formats (Gulf +03:00 default):
      DD/MM HH:MM        →  15/11 21:00
      DD/MM/YYYY HH:MM   →  15/11/2026 21:00
      +Xh                →  +48h  (X hours from now)
    Raises ValueError on bad input or a past date.
    """
    text = text.strip()
    tz = timezone(timedelta(hours=tz_offset))
    m = re.fullmatch(r"\+(\d+(?:\.\d+)?)h", text)
    if m:
        dt = datetime.now(tz) + timedelta(hours=float(m.group(1)))
        return dt.astimezone(timezone.utc)
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now(tz).year)
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date: {text!r}")


def _parse_event_datetime(text: str) -> datetime:
    """Parse an ISO-8601 event time with an explicit timezone offset."""
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("أدخل الموعد بصيغة ISO 8601 مع المنطقة الزمنية، مثل 2026-11-18T19:00+03:00.") from exc
    if value.tzinfo is None:
        raise ValueError("يجب تحديد المنطقة الزمنية للموعد، مثل +03:00 أو Z.")
    value = value.astimezone(timezone.utc)
    if value <= datetime.now(timezone.utc):
        raise ValueError("يجب أن يكون الموعد الجديد في المستقبل.")
    return value


def _event_end_datetime(starts_at: datetime, duration_hours: int) -> datetime:
    if not 1 <= duration_hours <= 168:
        raise ValueError("مدة الفعالية يجب أن تكون بين ساعة و168 ساعة.")
    return starts_at + timedelta(hours=duration_hours)


def _parse_questions(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [q for q in raw if q]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return [q.strip() for q in raw.splitlines() if q.strip()]
    return []

logger = logging.getLogger("AdSentry.EventHub")

# ──────────────────────────────────────────────────────────────────────────────
# Embed builders
# ──────────────────────────────────────────────────────────────────────────────

def _seats_bar(registered: int, capacity: int, width: int = 10) -> str:
    if capacity <= 0:
        return "░" * width
    filled = min(width, max(0, round(registered / capacity * width)))
    return "█" * filled + "░" * (width - filled)


def _event_embed(event: dict, registered: int = 0, waitlist: int = 0) -> discord.Embed:
    raw_start = event.get("starts_at")
    if isinstance(raw_start, str):
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    elif isinstance(raw_start, datetime):
        start = raw_start
    else:
        start = datetime.now(timezone.utc)
    ts = int(start.timestamp())
    status = event.get("status", "OPEN")
    colors = {
        "OPEN": discord.Color.from_rgb(88, 101, 242),
        "CLOSED": discord.Color.dark_grey(),
        "CANCELLED": discord.Color.red(),
    }
    embed = discord.Embed(
        title=f"🎪  {event.get('title', 'فعالية')}",
        description=event.get("description", ""),
        color=colors.get(status, discord.Color.blurple()),
    )
    if event.get("banner_url"):
        embed.set_image(url=event["banner_url"])
    embed.add_field(name="📅  الموعد", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=True)
    raw_end = event.get("ends_at")
    if isinstance(raw_end, str):
        end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
    elif isinstance(raw_end, datetime):
        end = raw_end
    else:
        end = None
    if end:
        embed.add_field(name="🕒  نهاية الفعالية", value=f"<t:{int(end.timestamp())}:F>", inline=True)
    cap = event.get("capacity", 0)
    spots_left = max(0, cap - registered)
    bar = _seats_bar(registered, cap)
    seats_val = f"`{registered}/{cap}` · متبقي **{spots_left}**\n{bar}"
    if spots_left == 0 and waitlist > 0:
        seats_val += f"\n⏳ {waitlist} في قائمة الانتظار"
    embed.add_field(name="🎟️  المقاعد", value=seats_val, inline=True)
    if event.get("location_channel_id"):
        embed.add_field(name="📍  المكان", value=f"<#{event['location_channel_id']}>", inline=True)
    elif event.get("location_text"):
        embed.add_field(name="📍  المكان", value=event["location_text"], inline=True)
    if event.get("prizes"):
        embed.add_field(name="🏆  الجوائز", value=event["prizes"], inline=False)
    reg_icon = "📝 بنموذج مخصص" if event.get("registration_type") == "FORM" else "⚡ تسجيل فوري"
    eid = event.get("id")
    id_tag = f"#{eid}  •  " if eid else ""
    footers = {
        "OPEN": f"{id_tag}{reg_icon}  •  AdSentry EventHub",
        "CLOSED": f"🔒  التسجيل مغلق  •  {id_tag}AdSentry EventHub",
        "CANCELLED": f"❌  الفعالية ملغاة  •  {id_tag}AdSentry EventHub",
    }
    embed.set_footer(text=footers.get(status, f"{id_tag}AdSentry EventHub"))
    return embed


def build_event_embed(
    title: str,
    description: str,
    start: datetime,
    capacity: int,
    registered: int = 0,
    *,
    event_id: int | None = None,
    organizer: discord.Member | discord.User | None = None,
    status: str = "OPEN",
    image_url: str | None = None,
    banner_url: str | None = None,
    ends_at: datetime | None = None,
    location: str | None = None,
    prizes: str | None = None,
    waitlist: int = 0,
) -> discord.Embed:
    """Build a public event announcement embed."""
    event_dict = {
        "id": event_id,
        "title": title,
        "description": description,
        "starts_at": start,
        "ends_at": ends_at,
        "capacity": capacity,
        "status": status,
        "banner_url": banner_url or image_url,
        "prizes": prizes,
    }
    if location:
        event_dict["location_text"] = location
    embed = _event_embed(event_dict, registered=registered, waitlist=waitlist)
    if organizer:
        embed.add_field(name="👤  المنظّم", value=organizer.mention, inline=True)
    return embed


def _admin_embed(event: dict, registered: int = 0, waitlist: int = 0, creator: str = "—") -> discord.Embed:
    raw_start = event.get("starts_at")
    if isinstance(raw_start, str):
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    elif isinstance(raw_start, datetime):
        start = raw_start
    else:
        start = datetime.now(timezone.utc)
    ts = int(start.timestamp())
    embed = discord.Embed(
        title=f"🛡️  إدارة الفعالية #{event['id']}  ·  {event['title']}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="📅  الموعد", value=f"<t:{ts}:F>", inline=True)
    raw_end = event.get("ends_at")
    if raw_end:
        end = datetime.fromisoformat(raw_end.replace("Z", "+00:00")) if isinstance(raw_end, str) else raw_end
        embed.add_field(name="🕒  النهاية", value=f"<t:{int(end.timestamp())}:F>", inline=True)
    embed.add_field(name="🎟️  المسجلون", value=f"{registered}/{event['capacity']}", inline=True)
    embed.add_field(name="⏳  انتظار", value=str(waitlist), inline=True)
    embed.add_field(name="📋  التسجيل", value="نموذج مخصص" if event.get("registration_type") == "FORM" else "فوري", inline=True)
    embed.add_field(name="👤  أنشأها", value=creator, inline=True)
    if event.get("location_channel_id"):
        embed.add_field(name="📍  المكان", value=f"<#{event['location_channel_id']}>", inline=True)
    elif event.get("location_text"):
        embed.add_field(name="📍  المكان", value=event["location_text"], inline=True)
    if event.get("prizes"):
        embed.add_field(name="🏆  الجوائز", value=event["prizes"], inline=False)
    questions = _parse_questions(event.get("form_questions") or [])
    if questions:
        embed.add_field(
            name="📝  أسئلة النموذج",
            value="\n".join(f"**{i+1}.** {q}" for i, q in enumerate(questions)),
            inline=False,
        )
    embed.set_footer(text="AdSentry EventHub  •  أزرار التحكم أدناه")
    return embed


def _confirm_embed(ev: dict) -> discord.Embed:
    raw_start = ev.get("starts_at")
    if isinstance(raw_start, str):
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    elif isinstance(raw_start, datetime):
        start = raw_start
    else:
        start = datetime.now(timezone.utc)
    ts = int(start.timestamp())
    e = discord.Embed(title=f"تأكيد التسجيل في: {ev['title']}", color=discord.Color.blurple())
    e.add_field(name="📅  الموعد", value=f"<t:{ts}:F>", inline=True)
    raw_end = ev.get("ends_at")
    if raw_end:
        end = datetime.fromisoformat(raw_end.replace("Z", "+00:00")) if isinstance(raw_end, str) else raw_end
        e.add_field(name="🕒  النهاية", value=f"<t:{int(end.timestamp())}:F>", inline=True)
    if ev.get("prizes"):
        e.add_field(name="🏆  الجوائز", value=ev["prizes"], inline=False)
    e.set_footer(text="اضغط تأكيد للتسجيل الفوري")
    return e


# ──────────────────────────────────────────────────────────────────────────────
# Role helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _ensure_event_roles(guild: discord.Guild) -> list[discord.Role]:
    lock = _EVENT_ROLE_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        return await _create_event_roles(guild)


async def ensure_event_roles(guild: discord.Guild) -> list[discord.Role]:
    return await _ensure_event_roles(guild)


async def _create_event_roles(guild: discord.Guild) -> list[discord.Role]:
    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        raise RuntimeError("AdSentry needs Manage Roles to provision event roles.")

    roles_by_name = {role.name: role for role in guild.roles}
    legacy_role = roles_by_name.get("🎉 Events")
    if legacy_role and _event_role_name(1) not in roles_by_name:
        try:
            await legacy_role.edit(name=_event_role_name(1), reason="AdSentry EventHub role migration")
            roles_by_name.pop(legacy_role.name, None)
            roles_by_name[_event_role_name(1)] = legacy_role
        except discord.HTTPException:
            logger.exception("Could not rename the legacy EventHub participant role in guild %s", guild.id)

    roles: list[discord.Role] = []
    for slot in range(1, len(EVENT_ROLE_NAMES) + 1):
        name = _event_role_name(slot)
        role = roles_by_name.get(name)
        if role is None:
            role = await guild.create_role(
                name=name,
                color=discord.Color.from_rgb(114, 137, 218),
                mentionable=False,
                reason="AdSentry EventHub participant role",
            )
            roles_by_name[name] = role
        if role >= bot_member.top_role:
            raise RuntimeError(f"AdSentry's highest role must be above {name}.")
        roles.append(role)
    return roles


async def _event_role(
    guild: discord.Guild,
    event_id: int,
    client: Any,
) -> discord.Role | None:
    rows = (
        await client.table("events")
        .select("role_slot,role_id")
        .eq("id", event_id)
        .eq("guild_id", guild.id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None

    event = rows[0]
    slot = event.get("role_slot")
    if slot is not None and 1 <= int(slot) <= len(EVENT_ROLE_NAMES):
        roles = await _ensure_event_roles(guild)
        role = roles[int(slot) - 1]
        if event.get("role_id") != role.id:
            await client.table("events").update({"role_id": role.id}).eq("id", event_id).execute()
        return role

    role_id = event.get("role_id")
    return guild.get_role(int(role_id)) if role_id else None


async def _grant_event_role(
    guild: discord.Guild,
    user_id: int,
    event_id: int,
    client: Any,
) -> discord.Role | None:
    try:
        role = await _event_role(guild, event_id, client)
    except Exception:
        logger.exception("Could not resolve participant role for event %s in guild %s", event_id, guild.id)
        return None
    if not role:
        logger.error("Could not resolve participant role for event %s in guild %s", event_id, guild.id)
        return None
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.exception("Could not fetch participant %s for event %s", user_id, event_id)
            return None
    if role not in member.roles:
        try:
            await member.add_roles(role, reason=f"Registered for EventHub event #{event_id}")
        except discord.HTTPException:
            logger.exception("Could not assign event role %s to member %s", role.id, user_id)
            return None
    return role


async def _revoke_event_role(
    guild: discord.Guild,
    user_id: int,
    event_id: int,
    client: Any,
) -> None:
    try:
        role = await _event_role(guild, event_id, client)
    except Exception:
        logger.exception("Could not resolve role to revoke for event %s in guild %s", event_id, guild.id)
        return
    member = guild.get_member(user_id)
    if role and member and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Cancelled EventHub event #{event_id} registration")
        except discord.HTTPException:
            logger.exception("Could not remove event role %s from member %s", role.id, user_id)


async def _strip_role_all(guild: discord.Guild, event_id: int, client: Any) -> None:
    try:
        role = await _event_role(guild, event_id, client)
    except Exception:
        logger.exception("Could not resolve role to remove for closed event %s", event_id)
        return
    if role is None:
        return
    rows = (
        await client.table("event_participants")
        .select("user_id")
        .eq("event_id", event_id)
        .eq("status", "REGISTERED")
        .execute()
    ).data or []
    for r in rows:
        user_id = int(r["user_id"])
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                continue
            except discord.HTTPException:
                logger.exception("Could not fetch participant %s while removing event %s role", user_id, event_id)
                continue
        if member and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"EventHub event #{event_id} closed")
            except discord.HTTPException:
                logger.exception("Could not remove event role %s from member %s", role.id, member.id)


async def remove_event_roles(guild: discord.Guild, event_id: int, client: Any) -> None:
    await _strip_role_all(guild, event_id, client)


# ──────────────────────────────────────────────────────────────────────────────
# Live embed refresh
# ──────────────────────────────────────────────────────────────────────────────

async def _refresh_embed(bot: commands.Bot, event_id: int) -> None:
    try:
        client = await bot.db.get_client()  # type: ignore[attr-defined]
        ev = (await client.table("events").select("*").eq("id", event_id).limit(1).execute()).data
        if not ev:
            return
        ev = ev[0]
        registered = (
            await client.table("event_participants").select("id", count="exact")
            .eq("event_id", event_id).eq("status", "REGISTERED").execute()
        ).count or 0
        waitlist = (
            await client.table("event_participants").select("id", count="exact")
            .eq("event_id", event_id).eq("status", "WAITLIST").execute()
        ).count or 0
        ch = bot.get_channel(int(ev["channel_id"]))
        if isinstance(ch, discord.TextChannel) and ev.get("announcement_message_id"):
            try:
                msg = await ch.fetch_message(int(ev["announcement_message_id"]))
                view = RegisterView(event_id, ev.get("registration_type", "DIRECT")) if ev["status"] == "OPEN" else None
                await msg.edit(embed=_event_embed(ev, registered, waitlist), view=view)
            except discord.NotFound:
                pass
        if ev.get("admin_channel_id") and ev.get("admin_message_id"):
            ach = bot.get_channel(int(ev["admin_channel_id"]))
            if isinstance(ach, discord.TextChannel):
                try:
                    amsg = await ach.fetch_message(int(ev["admin_message_id"]))
                    await amsg.edit(embed=_admin_embed(ev, registered, waitlist))
                except discord.NotFound:
                    pass
    except Exception:
        logger.exception("_refresh_embed failed for event %s", event_id)


# ──────────────────────────────────────────────────────────────────────────────
# Member-facing views
# ──────────────────────────────────────────────────────────────────────────────

class RegisterView(discord.ui.View):
    """Persistent public-announcement view: register + cancel buttons."""

    def __init__(self, event_id: int, reg_type: str = "DIRECT") -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        label = "📝  أدخل معلوماتك للتسجيل" if reg_type == "FORM" else "🎟️  سجّل في الفعالية"
        reg = discord.ui.Button(
            label=label, style=discord.ButtonStyle.success,
            custom_id=f"eh:reg:{event_id}",
        )
        reg.callback = self._on_register  # type: ignore[method-assign]
        self.add_item(reg)
        cancel = discord.ui.Button(
            label="❌  إلغاء تسجيلي", style=discord.ButtonStyle.secondary,
            custom_id=f"eh:cancel:{event_id}",
        )
        cancel.callback = self._on_cancel  # type: ignore[method-assign]
        self.add_item(cancel)

    async def _on_register(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("يجب استخدام الزر داخل السيرفر.", ephemeral=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        ev = (await client.table("events").select("*").eq("id", self.event_id).limit(1).execute()).data
        if not ev:
            return await interaction.response.send_message("⚠️  الفعالية غير موجودة.", ephemeral=True)
        ev = ev[0]
        if ev["status"] != "OPEN":
            return await interaction.response.send_message("🔒  التسجيل مغلق.", ephemeral=True)
        if ev.get("registration_type") == "FORM":
            qs = _parse_questions(ev.get("form_questions") or [])
            return await interaction.response.send_modal(FormModal(self.event_id, qs[:4]))
        await interaction.response.send_message(
            embed=_confirm_embed(ev),
            view=_ConfirmView(self.event_id),
            ephemeral=True,
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("يجب استخدام الزر داخل السيرفر.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        resp = await client.rpc("cancel_event_registration", {
            "p_event_id": self.event_id,
            "p_guild_id": interaction.guild.id,
            "p_user_id": interaction.user.id,
        }).execute()
        d = (resp.data[0] if isinstance(resp.data, list) else resp.data) or {}
        result = d.get("result", "error")
        if result == "not_registered":
            return await interaction.followup.send("ℹ️  أنت لست مسجلًا في هذه الفعالية أصلاً.", ephemeral=True)
        if result == "error":
            return await interaction.followup.send("⚠️  تعذر إلغاء تسجيلك، حاول مجدداً.", ephemeral=True)
        await _revoke_event_role(interaction.guild, interaction.user.id, self.event_id, client)
        promoted_id = d.get("promoted_user_id")
        if promoted_id:
            await _grant_event_role(interaction.guild, int(promoted_id), self.event_id, client)
            promo = interaction.guild.get_member(int(promoted_id))
            if promo:
                try:
                    await promo.send(embed=discord.Embed(
                        title="🎉  تمت ترقيتك من قائمة الانتظار!",
                        description=f"شغر مقعد وتمت إضافتك للمشاركين في **{interaction.guild.name}**!",
                        color=discord.Color.green(),
                    ))
                except discord.HTTPException:
                    pass
        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]
        await interaction.followup.send("✅  تم إلغاء تسجيلك بنجاح.", ephemeral=True)


class _ConfirmView(discord.ui.View):
    """Ephemeral confirm/abort step before direct registration."""

    def __init__(self, event_id: int) -> None:
        super().__init__(timeout=60)
        self.event_id = event_id

    @discord.ui.button(label="✅  تأكيد التسجيل", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("خطأ.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        resp = await client.rpc("register_event_participant_advanced", {
            "p_event_id": self.event_id, "p_guild_id": interaction.guild.id,
            "p_user_id": interaction.user.id, "p_user_name": str(interaction.user),
            "p_form_answers": {},
        }).execute()
        d = (resp.data[0] if isinstance(resp.data, list) else resp.data) or {}
        result = d.get("result", "error")
        role = None
        if result == "registered":
            role = await _grant_event_role(interaction.guild, interaction.user.id, self.event_id, client)
        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]
        self.stop()
        msgs = {
            "registered": (
                f"✅  تم تسجيلك! أُضيفت لك رتبة `{role.name}`."
                if role else "✅  تم تسجيلك، لكن تعذرت إضافة رتبة المسابقة؛ أبلغ الإدارة."
            ),
            "waitlist":   f"⏳  امتلأت المقاعد — أنت في قائمة الانتظار (المركز {d.get('position', '؟')}).",
            "duplicate":  "ℹ️  أنت مسجل مسبقًا في هذه الفعالية.",
            "full":       "❌  اكتملت المقاعد وقائمة الانتظار مغلقة.",
            "closed":     "🔒  التسجيل مغلق.",
            "suspended":  "🚫  أنت موقوف مؤقتاً عن الفعاليات.",
        }
        await interaction.edit_original_response(content=msgs.get(result, "تعذر إتمام التسجيل."), embed=None, view=None)

    @discord.ui.button(label="🔙  تراجع", style=discord.ButtonStyle.secondary)
    async def abort(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="تم الإلغاء.", embed=None, view=None)


class FormModal(discord.ui.Modal):
    """Dynamic modal with up to 4 admin-defined questions."""

    def __init__(self, event_id: int, questions: list[str]) -> None:
        super().__init__(title="📝  نموذج التسجيل")
        self.event_id = event_id
        self._qs = questions
        for i, q in enumerate(questions[:4]):
            self.add_item(discord.ui.TextInput(
                label=q[:45],
                style=discord.TextStyle.paragraph if i == len(questions) - 1 else discord.TextStyle.short,
                max_length=400, custom_id=f"q{i}",
            ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("خطأ.", ephemeral=True)
        answers = {
            self._qs[i]: self.children[i].value  # type: ignore[union-attr]
            for i in range(len(self._qs))
            if i < len(self.children) and isinstance(self.children[i], discord.ui.TextInput)
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        resp = await client.rpc("register_event_participant_advanced", {
            "p_event_id": self.event_id, "p_guild_id": interaction.guild.id,
            "p_user_id": interaction.user.id, "p_user_name": str(interaction.user),
            "p_form_answers": answers,
        }).execute()
        d = (resp.data[0] if isinstance(resp.data, list) else resp.data) or {}
        result = d.get("result", "error")
        if result == "pending_approval":
            ev = (await client.table("events").select("*").eq("id", self.event_id).limit(1).execute()).data
            if ev:
                await _send_application_card(
                    interaction.client, ev[0], interaction.user,  # type: ignore[arg-type]
                    answers, int(d.get("participant_id", 0)), interaction.guild,
                )
            return await interaction.followup.send(
                "📬  وصل طلبك للمشرفين! سنُعلمك بالقرار عبر الرسائل الخاصة.", ephemeral=True
            )
        if result == "registered":
            role = await _grant_event_role(interaction.guild, interaction.user.id, self.event_id, client)
        else:
            role = None
        msgs = {
            "registered": (
                f"✅  تم قبول تسجيلك مباشرةً! أُضيفت لك رتبة `{role.name}`."
                if role else "✅  تم قبول تسجيلك مباشرةً، لكن تعذرت إضافة رتبة المسابقة؛ أبلغ الإدارة."
            ),
            "waitlist":   f"⏳  أنت في قائمة الانتظار (المركز {d.get('position', '؟')}).",
            "suspended":  "🚫  أنت موقوف مؤقتاً عن الفعاليات.",
            "duplicate":  "ℹ️  طلبك قيد المراجعة أو أنت مسجل مسبقاً.",
            "closed":     "🔒  التسجيل مغلق.",
        }
        await interaction.followup.send(msgs.get(result, "تعذر إرسال النموذج."), ephemeral=True)


async def _send_application_card(
    bot: commands.Bot, ev: dict, user: discord.abc.User,
    answers: dict, participant_id: int, guild: discord.Guild,
) -> None:
    if not ev.get("admin_channel_id"):
        return
    ch = bot.get_channel(int(ev["admin_channel_id"]))
    if not isinstance(ch, discord.TextChannel):
        return
    embed = discord.Embed(title=f"📬  طلب تسجيل — {ev['title']}", color=discord.Color.orange())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="👤  المتقدم", value=f"{user.mention}\n{user}", inline=False)
    for q, a in answers.items():
        embed.add_field(name=q, value=a or "—", inline=False)
    embed.set_footer(text=f"الفعالية #{ev['id']}  •  AdSentry EventHub")
    view = ApplicationReviewView(int(ev["id"]), participant_id)
    try:
        msg = await ch.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        bot.add_view(view, message_id=msg.id)
    except discord.HTTPException:
        logger.exception("Could not send application card")


# ──────────────────────────────────────────────────────────────────────────────
# Staff-facing views
# ──────────────────────────────────────────────────────────────────────────────

async def _get_manageable_event(
    interaction: discord.Interaction,
    event_id: int,
    client: Any,
) -> dict | None:
    if not interaction.guild:
        return None
    rows = (
        await client.table("events")
        .select("*")
        .eq("id", event_id)
        .eq("guild_id", interaction.guild.id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None
    event = rows[0]
    if is_staff(interaction.user) or int(event["created_by"]) == interaction.user.id:
        return event
    return None


class EventEditModal(discord.ui.Modal, title="تعديل الفعالية"):
    def __init__(self, event: dict) -> None:
        super().__init__()
        self.event_id = int(event["id"])
        self.title_input = discord.ui.TextInput(
            label="اسم الفعالية",
            default=str(event["title"])[:100],
            min_length=1,
            max_length=100,
        )
        self.description_input = discord.ui.TextInput(
            label="وصف الفعالية",
            style=discord.TextStyle.paragraph,
            default=str(event["description"])[:1800],
            min_length=1,
            max_length=1800,
        )
        self.capacity_input = discord.ui.TextInput(
            label="عدد المقاعد الجديد (1–500)",
            default=str(event["capacity"]),
            min_length=1,
            max_length=3,
        )
        starts_at = datetime.fromisoformat(str(event["starts_at"]).replace("Z", "+00:00"))
        raw_ends_at = event.get("ends_at")
        ends_at = (
            datetime.fromisoformat(str(raw_ends_at).replace("Z", "+00:00"))
            if raw_ends_at else starts_at + timedelta(hours=2)
        )
        self.starts_at_input = discord.ui.TextInput(
            label="موعد الفعالية الجديد (ISO 8601)",
            placeholder="2026-11-18T19:00+03:00",
            default=starts_at.isoformat(),
            min_length=16,
            max_length=40,
        )
        duration_hours = max(1, min(168, int((ends_at - starts_at).total_seconds() // 3600)))
        self.duration_input = discord.ui.TextInput(
            label="مدة الفعالية (1–168 ساعة)",
            default=str(duration_hours),
            min_length=1,
            max_length=3,
        )
        for item in (self.title_input, self.description_input, self.capacity_input, self.starts_at_input, self.duration_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم هذا النموذج داخل السيرفر.", ephemeral=True)
        title = self.title_input.value.strip()
        description = self.description_input.value.strip()
        if not title or not description:
            return await interaction.response.send_message("اسم الفعالية ووصفها لا يمكن أن يكونا فارغين.", ephemeral=True)
        try:
            capacity = int(self.capacity_input.value.strip())
            if not 1 <= capacity <= 500:
                raise ValueError
            starts_at = _parse_event_datetime(self.starts_at_input.value)
            duration_hours = int(self.duration_input.value.strip())
            ends_at = _event_end_datetime(starts_at, duration_hours)
        except ValueError as exc:
            message = str(exc) or "عدد المقاعد يجب أن يكون رقمًا بين 1 و500."
            return await interaction.response.send_message(message, ephemeral=True)

        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        event = await _get_manageable_event(interaction, self.event_id, client)
        if not event:
            return await interaction.response.send_message("ليس لديك صلاحية تعديل هذه الفعالية، أو أنها غير موجودة.", ephemeral=True)
        if event["status"] != "OPEN":
            return await interaction.response.send_message("لا يمكن تعديل فعالية مغلقة أو ملغاة.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            response = await client.rpc("update_event_details", {
                "p_event_id": self.event_id,
                "p_guild_id": interaction.guild.id,
                "p_title": title,
                "p_description": description,
                "p_capacity": capacity,
                "p_starts_at": starts_at.isoformat(),
                "p_ends_at": ends_at.isoformat(),
            }).execute()
            result = response.data[0] if isinstance(response.data, list) else response.data
            if not isinstance(result, dict):
                raise RuntimeError("Event update returned an invalid database response.")
            if not result.get("updated"):
                if result.get("reason") == "capacity":
                    registered = int(result.get("registered_count", 0))
                    await interaction.followup.send(
                        f"لا يمكن خفض السعة إلى {capacity}؛ يوجد {registered} عضو مسجل حاليًا. يجب أن تكون السعة مساوية لعدد المسجلين على الأقل.",
                        ephemeral=True,
                    )
                    return
                if result.get("reason") == "closed":
                    await interaction.followup.send("لا يمكن تعديل فعالية مغلقة أو ملغاة.", ephemeral=True)
                    return
                if result.get("reason") == "not_found":
                    await interaction.followup.send("الفعالية غير موجودة في هذا السيرفر.", ephemeral=True)
                    return
                raise RuntimeError(f"Event update failed: {result.get('reason')}")
        except Exception:
            logger.exception("Could not update EventHub event %s", self.event_id)
            await interaction.followup.send(
                "تعذر حفظ تعديلات الفعالية. تحقق من تطبيق ترحيل `eventhub_event_duration_migration.sql` ثم حاول مجددًا.",
                ephemeral=True,
            )
            return

        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]
        await interaction.followup.send(
            "✅ تم تحديث عنوان ووصف وموعد ومدة وسعة الفعالية، وتم تحديث إعلانها المنشور.",
            ephemeral=True,
        )


class EventCancelModal(discord.ui.Modal, title="إلغاء الفعالية"):
    def __init__(self, event: dict) -> None:
        super().__init__()
        self.event_id = int(event["id"])
        self.reason_input = discord.ui.TextInput(
            label="سبب الإلغاء (اختياري)",
            style=discord.TextStyle.paragraph,
            placeholder="اكتب سببًا مختصرًا لإظهاره للمشاركين",
            required=False,
            max_length=500,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم هذا النموذج داخل السيرفر.", ephemeral=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        event = await _get_manageable_event(interaction, self.event_id, client)
        if not event:
            return await interaction.response.send_message("ليس لديك صلاحية إلغاء هذه الفعالية، أو أنها غير موجودة.", ephemeral=True)
        if event["status"] != "OPEN":
            return await interaction.response.send_message("الفعالية مغلقة أو ملغاة مسبقًا.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            response = await client.rpc("cancel_event", {
                "p_event_id": self.event_id,
                "p_guild_id": interaction.guild.id,
            }).execute()
            result = response.data[0] if isinstance(response.data, list) else response.data
            if not isinstance(result, dict):
                raise RuntimeError("Event cancellation returned an invalid database response.")
            if not result.get("cancelled"):
                await interaction.followup.send(
                    "الفعالية غير موجودة أو لم تعد مفتوحة.",
                    ephemeral=True,
                )
                return
            event = result["event"]
        except Exception:
            logger.exception("Could not cancel EventHub event %s", self.event_id)
            await interaction.followup.send("تعذر إلغاء الفعالية. تحقق من تطبيق ترحيل EventHub الأخير.", ephemeral=True)
            return

        await _strip_role_all(interaction.guild, self.event_id, client)
        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]

        channel = interaction.client.get_channel(int(event["channel_id"]))  # type: ignore[attr-defined]
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await interaction.client.fetch_channel(int(event["channel_id"]))  # type: ignore[attr-defined]
            except discord.HTTPException:
                channel = None

        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title=f"❌ تم إلغاء الفعالية: {event['title']}",
                description="نعتذر للمشاركين، تم إلغاء هذه الفعالية ولن تُقام في موعدها.",
                color=discord.Color.red(),
            )
            if self.reason_input.value.strip():
                embed.add_field(name="سبب الإلغاء", value=self.reason_input.value.strip(), inline=False)
            embed.set_footer(text=f"AdSentry EventHub · الفعالية #{self.event_id}")
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.exception("Event %s was cancelled but its public notice could not be sent", self.event_id)
                await interaction.followup.send(
                    "تم إلغاء الفعالية، لكن تعذر إرسال إشعار الإلغاء إلى قناة الإعلان. تحقق من صلاحيات البوت في القناة.",
                    ephemeral=True,
                )
                return
        else:
            await interaction.followup.send(
                "تم إلغاء الفعالية، لكن تعذر الوصول إلى قناة إعلانها لإرسال إشعار الإلغاء.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ تم إلغاء **{event['title']}** وإرسال إشعار الإلغاء في {channel.mention}.",
            ephemeral=True,
        )


class AdminView(discord.ui.View):
    """Persistent admin control card for an EventHub event."""

    def __init__(self, event_id: int) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        defs = [
            ("🔒  إغلاق التسجيل",   discord.ButtonStyle.danger,    "close",   self._close,   0),
            ("📢  تذكير فوري",       discord.ButtonStyle.primary,   "remind",  self._remind,  0),
            ("📋  قائمة المشاركين",  discord.ButtonStyle.secondary, "list",    self._list,    0),
            ("✅  تسجيل الحضور",     discord.ButtonStyle.secondary, "attend",  self._attend,  1),
            ("📥  تصدير CSV",        discord.ButtonStyle.secondary, "export",  self._export,  1),
            ("🏆  إعلان الفائزين",   discord.ButtonStyle.success,   "winners", self._winners, 1),
            ("✏️  تعديل الفعالية",   discord.ButtonStyle.primary,   "edit",    self._edit,    2),
            ("❌  إلغاء الفعالية",   discord.ButtonStyle.danger,    "cancel",  self._cancel,  2),
        ]
        for label, style, key, cb, row in defs:
            btn = discord.ui.Button(label=label, style=style, custom_id=f"eh:admin:{key}:{event_id}", row=row)
            btn.callback = cb  # type: ignore[method-assign]
            self.add_item(btn)

    def _ok(self, i: discord.Interaction) -> bool:
        return bool(i.guild and is_staff(i.user))

    async def _edit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم هذا الزر داخل السيرفر.", ephemeral=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        event = await _get_manageable_event(interaction, self.event_id, client)
        if not event:
            return await interaction.response.send_message("تعديل الفعالية متاح لمنشئها أو للإدارة فقط.", ephemeral=True)
        if event["status"] != "OPEN":
            return await interaction.response.send_message("لا يمكن تعديل فعالية مغلقة أو ملغاة.", ephemeral=True)
        await interaction.response.send_modal(EventEditModal(event))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم هذا الزر داخل السيرفر.", ephemeral=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        event = await _get_manageable_event(interaction, self.event_id, client)
        if not event:
            return await interaction.response.send_message("إلغاء الفعالية متاح لمنشئها أو للإدارة فقط.", ephemeral=True)
        if event["status"] != "OPEN":
            return await interaction.response.send_message("الفعالية مغلقة أو ملغاة مسبقًا.", ephemeral=True)
        await interaction.response.send_modal(EventCancelModal(event))

    async def _close(self, interaction: discord.Interaction) -> None:
        if not self._ok(interaction):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        now = datetime.now(timezone.utc).isoformat()
        res = (
            await client.table("events").update({"status": "CLOSED", "closed_at": now})
            .eq("id", self.event_id).eq("status", "OPEN").execute()
        ).data
        if not res:
            return await interaction.followup.send("الفعالية مغلقة مسبقاً.", ephemeral=True)
        await _strip_role_all(interaction.guild, self.event_id, client)  # type: ignore[arg-type]
        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]
        await interaction.followup.send("🔒  تم إغلاق التسجيل وسحب رتبة الفعالية من جميع المشاركين.", ephemeral=True)

    async def _remind(self, interaction: discord.Interaction) -> None:
        if not self._ok(interaction):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        ev = (await client.table("events").select("title,starts_at,channel_id,capacity").eq("id", self.event_id).limit(1).execute()).data
        if not ev:
            return await interaction.followup.send("الفعالية غير موجودة.", ephemeral=True)
        ev = ev[0]
        ch = interaction.client.get_channel(int(ev["channel_id"]))  # type: ignore[attr-defined]
        if isinstance(ch, discord.TextChannel):
            ts = int(datetime.fromisoformat(ev["starts_at"].replace("Z", "+00:00")).timestamp())
            count = (
                await client.table("event_participants").select("id", count="exact")
                .eq("event_id", self.event_id).eq("status", "REGISTERED").execute()
            ).count or 0
            await ch.send(
                embed=discord.Embed(
                    title=f"⏰  تذكير · {ev['title']}",
                    description=f"تبدأ الفعالية <t:{ts}:R>!\nالمسجلون: `{count}/{ev['capacity']}`",
                    color=discord.Color.gold(),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await interaction.followup.send("✅  تم إرسال التذكير.", ephemeral=True)

    async def _list(self, interaction: discord.Interaction) -> None:
        if not self._ok(interaction):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        rows = (
            await client.table("event_participants")
            .select("user_id,user_name,status,attended")
            .eq("event_id", self.event_id)
            .in_("status", ["REGISTERED", "WAITLIST", "PENDING"])
            .order("created_at").limit(25).execute()
        ).data or []
        if not rows:
            return await interaction.followup.send("لا يوجد مشاركون مسجلون بعد.", ephemeral=True)
        icons = {"REGISTERED": "✅", "WAITLIST": "⏳", "PENDING": "🕒"}
        lines = [f"{icons.get(r['status'], '❓')} <@{r['user_id']}> · {r['user_name']}" for r in rows]
        embed = discord.Embed(
            title=f"📋  مشاركو الفعالية #{self.event_id}",
            description="\n".join(lines), color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"المجموع: {len(rows)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _attend(self, interaction: discord.Interaction) -> None:
        if not self._ok(interaction):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        rows = (
            await client.table("event_participants")
            .select("user_id,user_name")
            .eq("event_id", self.event_id).eq("status", "REGISTERED")
            .limit(25).execute()
        ).data or []
        if 0 < len(rows) <= 25:
            return await interaction.response.send_message(
                "✅  اختر الأعضاء **الحاضرين** من القائمة:",
                view=AttendanceSelectView(self.event_id, rows), ephemeral=True,
            )
        await interaction.response.send_modal(BulkAttendanceModal(self.event_id))

    async def _export(self, interaction: discord.Interaction) -> None:
        if not self._ok(interaction):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        rows = (
            await client.table("event_participants")
            .select("user_id,user_name,status,attended,created_at,form_answers")
            .eq("event_id", self.event_id).order("created_at").execute()
        ).data or []
        sample: dict = {}
        for r in rows:
            a = r.get("form_answers") or {}
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except Exception:
                    a = {}
            if a:
                sample = a
                break
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["user_id", "user_name", "status", "attended", "registered_at"] + list(sample.keys()),
            extrasaction="ignore",
        )
        writer.writeheader()
        for r in rows:
            ans = r.get("form_answers") or {}
            if isinstance(ans, str):
                try:
                    ans = json.loads(ans)
                except Exception:
                    ans = {}
            writer.writerow({
                "user_id": r["user_id"], "user_name": r["user_name"],
                "status": r["status"], "attended": r.get("attended"),
                "registered_at": r.get("created_at", ""), **ans,
            })
        buf.seek(0)
        f = discord.File(io.BytesIO(buf.read().encode("utf-8-sig")), filename=f"event_{self.event_id}.csv")
        await interaction.followup.send(f"📥  كشف المشاركين ({len(rows)} شخص):", file=f, ephemeral=True)

    async def _winners(self, interaction: discord.Interaction) -> None:
        if not self._ok(interaction):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.send_modal(WinnersModal(self.event_id))


class AttendanceSelectView(discord.ui.View):
    """Select-menu attendance — staff ticks who attended, no ID typing."""

    def __init__(self, event_id: int, participants: list[dict]) -> None:
        super().__init__(timeout=120)
        self.event_id = event_id
        self.participants = participants
        self._selected: list[str] = []
        opts = [
            discord.SelectOption(label=p["user_name"][:100], value=str(p["user_id"]), emoji="👤")
            for p in participants
        ]
        sel = discord.ui.Select(
            placeholder="اختر الأعضاء الحاضرين...",
            options=opts, min_values=0, max_values=len(opts),
            custom_id=f"eh:attend_sel:{event_id}",
        )
        sel.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(sel)
        done = discord.ui.Button(
            label="✅  تأكيد الحضور وتسجيل الغيابات",
            style=discord.ButtonStyle.success,
            custom_id=f"eh:attend_done:{event_id}",
        )
        done.callback = self._on_done  # type: ignore[method-assign]
        self.add_item(done)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self._selected = interaction.data["values"]  # type: ignore[index]
        await interaction.response.edit_message(
            content=f"✅  اخترت **{len(self._selected)}** حاضر من أصل {len(self.participants)}. اضغط تأكيد عند الانتهاء."
        )

    async def _on_done(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("خطأ.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        now = datetime.now(timezone.utc).isoformat()
        attended_ids = set(self._selected)
        all_ids = {str(p["user_id"]) for p in self.participants}
        absent_ids = all_ids - attended_ids
        updated = 0
        for uid in attended_ids:
            r = (
                await client.table("event_participants")
                .update({"attended": True, "attendance_by": interaction.user.id, "attendance_at": now})
                .eq("event_id", self.event_id).eq("user_id", int(uid)).execute()
            ).data
            if r:
                updated += 1
        suspended: list[int] = []
        for uid in absent_ids:
            resp = await client.rpc("record_event_no_show", {
                "p_guild_id": interaction.guild.id, "p_event_id": self.event_id,
                "p_user_id": int(uid), "p_recorded_by": interaction.user.id,
            }).execute()
            d = (resp.data[0] if isinstance(resp.data, list) else resp.data) or {}
            if d.get("suspended"):
                suspended.append(int(uid))
                try:
                    until_ts = int(datetime.fromisoformat(d["suspended_until"].replace("Z", "+00:00")).timestamp())
                    u = await interaction.client.fetch_user(int(uid))  # type: ignore[attr-defined]
                    await u.send(embed=discord.Embed(
                        title="⚠️  إيقاف مؤقت عن الفعاليات",
                        description=f"غبت عن 3 فعاليات في **{interaction.guild.name}**.\nإيقاف حتى <t:{until_ts}:F>.",
                        color=discord.Color.red(),
                    ))
                except Exception:
                    pass
        self.stop()
        summary = f"✅  حضور: {updated}  |  غياب: {len(absent_ids)}"
        if suspended:
            summary += f"\n🚫  تم إيقاف {len(suspended)} عضو بسبب تكرار الغياب."
        await interaction.edit_original_response(content=summary, view=None)


class ApplicationReviewView(discord.ui.View):
    def __init__(self, event_id: int, participant_id: int) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        self.participant_id = participant_id
        a = discord.ui.Button(label="✅  قبول", style=discord.ButtonStyle.success, custom_id=f"eh:app:accept:{participant_id}")
        a.callback = self._accept  # type: ignore[method-assign]
        self.add_item(a)
        r = discord.ui.Button(label="❌  رفض", style=discord.ButtonStyle.danger, custom_id=f"eh:app:reject:{participant_id}")
        r.callback = self._reject  # type: ignore[method-assign]
        self.add_item(r)

    async def _accept(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        ev = (await client.table("events").select("capacity,title").eq("id", self.event_id).limit(1).execute()).data
        if not ev:
            return await interaction.followup.send("الفعالية غير موجودة.")
        ev = ev[0]
        count = (
            await client.table("event_participants").select("id", count="exact")
            .eq("event_id", self.event_id).eq("status", "REGISTERED").execute()
        ).count or 0
        new_status = "REGISTERED" if count < ev["capacity"] else "WAITLIST"
        res = (
            await client.table("event_participants")
            .update({"status": new_status, "reviewed_by": interaction.user.id})
            .eq("id", self.participant_id).eq("status", "PENDING").execute()
        ).data
        if not res:
            return await interaction.followup.send("الطلب مراجع مسبقاً.")
        p = res[0]
        role = None
        if new_status == "REGISTERED":
            role = await _grant_event_role(interaction.guild, int(p["user_id"]), self.event_id, client)
        try:
            u = await interaction.client.fetch_user(int(p["user_id"]))  # type: ignore[attr-defined]
            await u.send(embed=discord.Embed(
                title="🎉  تم قبولك في الفعالية!",
                description=f"تمت الموافقة على تسجيلك في **{ev['title']}**" +
                            ("\nأنت في قائمة الانتظار." if new_status == "WAITLIST" else ""),
                color=discord.Color.green(),
            ))
        except discord.HTTPException:
            pass
        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]
        if interaction.message and interaction.message.embeds:
            e = interaction.message.embeds[0]
            e.color = discord.Color.green()
            e.set_footer(text=f"✅  قبل من {interaction.user}  •  {new_status}")
            await interaction.message.edit(embed=e, view=None)
        role_notice = (
            f" أُضيفت رتبة `{role.name}`."
            if role else " تعذرت إضافة رتبة المسابقة؛ تحقق من صلاحيات الرتب."
        ) if new_status == "REGISTERED" else ""
        await interaction.followup.send(f"✅  قُبل الطلب كـ{new_status}.{role_notice}")

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.send_modal(RejectModal(self.event_id, self.participant_id))


# ──────────────────────────────────────────────────────────────────────────────
# Modals
# ──────────────────────────────────────────────────────────────────────────────

class EventHubTimePickerView(discord.ui.View):
    """Interactive Date & Time Picker using Discord Select Menus for EventHub."""

    def __init__(
        self,
        announce_channel: discord.TextChannel,
        admin_channel: discord.TextChannel | None,
        title: str,
        description: str,
        capacity: int,
        reg_type: str,
        questions: list[str],
        prizes: str | None,
        duration_hours: int,
    ) -> None:
        super().__init__(timeout=600)
        self.announce_channel = announce_channel
        self.admin_channel = admin_channel
        self.title = title
        self.description = description
        self.capacity = capacity
        self.reg_type = reg_type
        self.questions = questions
        self.prizes = prizes
        self.duration_hours = duration_hours

        self.tz = timezone(timedelta(hours=3))
        now_local = datetime.now(self.tz)

        tomorrow = (now_local + timedelta(days=1)).date()
        self.selected_date = tomorrow.isoformat()
        self.selected_time = "20:00"

        self._build_date_select(now_local)
        self._build_time_select()
        self._build_preset_select()

    def _build_date_select(self, now_local: datetime) -> None:
        arabic_days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        arabic_months = [
            "", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
            "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
        ]
        options = []
        for i in range(14):
            day = (now_local + timedelta(days=i)).date()
            weekday_name = arabic_days[day.weekday()]
            month_name = arabic_months[day.month]
            tag = " (اليوم)" if i == 0 else " (غداً)" if i == 1 else " (عطلة نهاية الأسبوع)" if day.weekday() in (4, 5) else ""
            label = f"{weekday_name} {day.day} {month_name}{tag}"
            options.append(discord.SelectOption(
                label=label[:100],
                value=day.isoformat(),
                description=f"التاريخ: {day.isoformat()}",
                default=(day.isoformat() == self.selected_date),
            ))
        self.date_select = discord.ui.Select(
            placeholder="📅 اختر تاريخ الفعالية من القائمة",
            options=options,
            row=0,
        )
        self.date_select.callback = self._on_date_change
        self.add_item(self.date_select)

    def _build_time_select(self) -> None:
        time_choices = [
            ("16:00", "04:00 PM (4:00 عصراً)"),
            ("17:00", "05:00 PM (5:00 مساءً)"),
            ("18:00", "06:00 PM (6:00 مساءً - المغرب)"),
            ("19:00", "07:00 PM (7:00 مساءً)"),
            ("20:00", "08:00 PM (8:00 مساءً - العشاء) [وقت مقترح]"),
            ("21:00", "09:00 PM (9:00 مساءً - وقت الذروة)"),
            ("22:00", "10:00 PM (10:00 مساءً)"),
            ("23:00", "11:00 PM (11:00 مساءً)"),
            ("00:00", "12:00 AM (12:00 منتصف الليل)"),
            ("01:00", "01:00 AM (1:00 فجراً)"),
            ("13:00", "01:00 PM (1:00 ظهراً)"),
            ("14:00", "02:00 PM (2:00 ظهراً)"),
            ("15:00", "03:00 PM (3:00 عصراً)"),
            ("10:00", "10:00 AM (10:00 صباحاً)"),
            ("11:00", "11:00 AM (11:00 صباحاً)"),
            ("12:00", "12:00 PM (12:00 ظهراً)"),
        ]
        options = [
            discord.SelectOption(label=label, value=val, default=(val == self.selected_time))
            for val, label in time_choices
        ]
        self.time_select = discord.ui.Select(
            placeholder="⏰ اختر ساعة وتوقيت الفعالية من القائمة",
            options=options,
            row=1,
        )
        self.time_select.callback = self._on_time_change
        self.add_item(self.time_select)

    def _build_preset_select(self) -> None:
        preset_choices = [
            ("+2h", "⚡ بعد ساعتين من الآن"),
            ("+4h", "⚡ بعد 4 ساعات من الآن"),
            ("+6h", "⚡ بعد 6 ساعات من الآن"),
            ("tonight_9pm", "🌙 الليلة الساعة 9:00 مساءً"),
            ("tomorrow_8pm", "📅 غداً الساعة 8:00 مساءً"),
            ("weekend_8pm", "🎉 نهاية الأسبوع (الجمعة 8:30 مساءً)"),
        ]
        options = [
            discord.SelectOption(label=label, value=val)
            for val, label in preset_choices
        ]
        self.preset_select = discord.ui.Select(
            placeholder="⚡ خيارات سريعة جاهزة (اختياري)",
            options=options,
            row=2,
        )
        self.preset_select.callback = self._on_preset_change
        self.add_item(self.preset_select)

    def get_selected_datetime(self) -> datetime:
        d = datetime.fromisoformat(self.selected_date).date()
        h, m = map(int, self.selected_time.split(":"))
        local_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
        return local_dt.astimezone(timezone.utc)

    def get_end_datetime(self) -> datetime:
        return _event_end_datetime(self.get_selected_datetime(), self.duration_hours)

    def render_content(self) -> str:
        dt_utc = self.get_selected_datetime()
        ts = int(dt_utc.timestamp())
        end_ts = int(self.get_end_datetime().timestamp())
        admin_info = f" · إشعارات الإدارة: {self.admin_channel.mention}" if self.admin_channel else ""
        return (
            f"🎪 **تحديد موعد الفعالية: {self.title}**\n\n"
            f"📅 **البداية:** <t:{ts}:F> (<t:{ts}:R>)\n"
            f"🕒 **النهاية:** <t:{end_ts}:F> · المدة `{self.duration_hours}` ساعة\n"
            f"👥 **المقاعد:** `{self.capacity}`  ·  📢 **القناة:** {self.announce_channel.mention}{admin_info}\n\n"
            "👇 **حدد التاريخ والساعة المناسبة من القوائم أدناه، ثم اضغط على زر التأكيد:**"
        )

    def _refresh_defaults(self) -> None:
        for opt in self.date_select.options:
            opt.default = (opt.value == self.selected_date)
        for opt in self.time_select.options:
            opt.default = (opt.value == self.selected_time)

    async def _on_date_change(self, interaction: discord.Interaction) -> None:
        self.selected_date = self.date_select.values[0]
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_time_change(self, interaction: discord.Interaction) -> None:
        self.selected_time = self.time_select.values[0]
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_preset_change(self, interaction: discord.Interaction) -> None:
        val = self.preset_select.values[0]
        now_local = datetime.now(self.tz)
        if val == "+2h":
            target = now_local + timedelta(hours=2)
        elif val == "+4h":
            target = now_local + timedelta(hours=4)
        elif val == "+6h":
            target = now_local + timedelta(hours=6)
        elif val == "tonight_9pm":
            target = now_local.replace(hour=21, minute=0, second=0, microsecond=0)
            if target <= now_local:
                target += timedelta(days=1)
        elif val == "tomorrow_8pm":
            target = (now_local + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
        elif val == "weekend_8pm":
            days_ahead = (4 - now_local.weekday()) % 7
            if days_ahead == 0 and now_local.hour >= 20:
                days_ahead = 7
            target = (now_local + timedelta(days=days_ahead)).replace(hour=20, minute=30, second=0, microsecond=0)
        else:
            target = now_local + timedelta(hours=24)

        self.selected_date = target.date().isoformat()
        self.selected_time = f"{target.hour:02d}:00"
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    @discord.ui.button(label="تأكيد ونشر الفعالية 🚀", style=discord.ButtonStyle.success, row=3)
    async def confirm_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        start_utc = self.get_selected_datetime()
        end_utc = self.get_end_datetime()
        if start_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message("❌ الموعد المحدد أصبح في الماضي؛ يرجى اختيار موعد قادم.", ephemeral=True)
            return

        if not interaction.guild:
            await interaction.response.send_message("يجب إنشاء الفعالية داخل السيرفر.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            roles = await _ensure_event_roles(interaction.guild)
        except Exception:
            logger.exception("Could not provision EventHub participant roles for guild %s", interaction.guild.id)
            await interaction.followup.send(
                "تعذر تجهيز رتب المسابقات. تأكد أن للبوت صلاحية **Manage Roles** وأن رتبته أعلى من رتب Event.",
                ephemeral=True,
            )
            return

        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        try:
            response = await client.rpc("create_event_with_slot", {
                "p_guild_id": interaction.guild.id,
                "p_created_by": interaction.user.id,
                "p_title": self.title[:100],
                "p_description": self.description[:1500],
                "p_channel_id": self.announce_channel.id,
                "p_starts_at": start_utc.isoformat(),
                "p_ends_at": end_utc.isoformat(),
                "p_capacity": self.capacity,
                "p_reminder_at": (start_utc - timedelta(hours=24)).isoformat(),
                "p_registration_type": self.reg_type,
                "p_form_questions": self.questions,
                "p_prizes": self.prizes,
                "p_admin_channel_id": self.admin_channel.id if self.admin_channel else None,
                "p_role_ids": [role.id for role in roles],
            }).execute()
            result = response.data[0] if isinstance(response.data, list) else response.data
            if not isinstance(result, dict):
                raise RuntimeError("Event creation returned an invalid database response.")
            if not result.get("created"):
                if result.get("reason") == "limit":
                    await interaction.followup.send(
                        "وصل السيرفر إلى الحد الأقصى: توجد خمس فعاليات/مسابقات مفتوحة بالفعل. أغلق واحدة قبل إنشاء غيرها.",
                        ephemeral=True,
                    )
                    return
                raise RuntimeError(f"Event creation was rejected: {result.get('reason')}")
            ev = result.get("event")
            if not isinstance(ev, dict):
                raise RuntimeError("The database did not return the created event.")
        except Exception:
            logger.exception("Could not create EventHub event for guild %s", interaction.guild.id)
            await interaction.followup.send(
                "تعذر إنشاء المسابقة. تحقق من تطبيق ترحيل `eventhub_event_duration_migration.sql` ثم حاول مجددًا.",
                ephemeral=True,
            )
            return

        event_id = int(ev["id"])
        pub_view = RegisterView(event_id, self.reg_type)
        msg: discord.Message | None = None
        try:
            msg = await self.announce_channel.send(
                embed=_event_embed(ev),
                view=pub_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await client.table("events").update(
                {"announcement_message_id": msg.id}
            ).eq("id", event_id).execute()
        except Exception:
            logger.exception("Could not publish EventHub announcement for event %s", event_id)
            if msg:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    logger.exception("Could not remove incomplete announcement for event %s", event_id)
            try:
                await client.table("events").update({
                    "status": "CANCELLED",
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", event_id).eq("status", "OPEN").execute()
            except Exception:
                logger.exception("Could not release the reserved event slot for event %s", event_id)
                await interaction.followup.send(
                    "تعذر نشر الإعلان، كما تعذر تحرير خانة المسابقة تلقائيًا. أبلغ الإدارة لإغلاق الطلب من قاعدة البيانات.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                "تعذر نشر إعلان المسابقة في القناة المحددة؛ أُلغيت عملية الإنشاء حتى لا يبقى مقعد مفتوح محجوزًا.",
                ephemeral=True,
            )
            return

        interaction.client.add_view(pub_view)  # type: ignore[attr-defined]
        if self.admin_channel:
            try:
                adm_view = AdminView(event_id)
                amsg = await self.admin_channel.send(
                    embed=_admin_embed(ev, creator=str(interaction.user)),
                    view=adm_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                interaction.client.add_view(adm_view)  # type: ignore[attr-defined]
                await client.table("events").update(
                    {"admin_message_id": amsg.id}
                ).eq("id", event_id).execute()
            except Exception:
                logger.exception("Event %s was published but its admin control card could not be delivered", event_id)

        ts = int(start_utc.timestamp())
        role_name = _event_role_name(int(ev["role_slot"]))
        await interaction.edit_original_response(
            content=f"🎉 **تم إنشاء الفعالية/المسابقة #{event_id} ونشرها بنجاح في {self.announce_channel.mention}**!\n"
                    f"🏷️ **رتبة المشاركين:** `{role_name}`\n"
                    f"📅 **البداية:** <t:{ts}:F> (<t:{ts}:R>)\n"
                    f"🕒 **النهاية:** <t:{int(end_utc.timestamp())}:F>",
            view=None,
        )

    @discord.ui.button(label="إلغاء ❌", style=discord.ButtonStyle.secondary, row=3)
    async def cancel_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="❌ تم إلغاء إنشاء الفعالية.", view=None)


class EventCreateModal(discord.ui.Modal, title="🎪  إنشاء فعالية جديدة"):
    """Staff modal — fills metadata and opens interactive DateTimePicker."""

    f_title    = discord.ui.TextInput(label="اسم الفعالية", max_length=100, placeholder="مثال: بطولة فوت سيك")
    f_desc     = discord.ui.TextInput(label="الوصف", style=discord.TextStyle.paragraph, max_length=1500, placeholder="تفاصيل الفعالية...")
    f_capacity = discord.ui.TextInput(label="عدد المقاعد", max_length=4, default="25", placeholder="25")
    f_duration = discord.ui.TextInput(
        label="مدة الفعالية بالساعات (1–168)",
        max_length=3,
        default="2",
        placeholder="2",
    )
    f_extras   = discord.ui.TextInput(
        label="إضافات — سطر الجوائز: ... ثم أسئلة النموذج",
        style=discord.TextStyle.paragraph, max_length=600, required=False,
        placeholder="جوائز: 200 ريال للأول\nما اسم لعبتك المفضلة؟",
    )

    def __init__(self, announce_channel: discord.TextChannel, admin_channel: discord.TextChannel | None = None) -> None:
        super().__init__()
        self.announce_channel = announce_channel
        self.admin_channel = admin_channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        try:
            capacity = max(1, min(500, int(self.f_capacity.value.strip())))
        except ValueError:
            return await interaction.response.send_message("❌  عدد المقاعد يجب أن يكون رقماً بين 1 و500.", ephemeral=True)
        try:
            duration_hours = int(self.f_duration.value.strip())
            if not 1 <= duration_hours <= 168:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌  مدة الفعالية يجب أن تكون بين ساعة و168 ساعة.", ephemeral=True)

        prizes: str | None = None
        questions: list[str] = []
        if self.f_extras.value:
            for line in self.f_extras.value.splitlines():
                line = line.strip()
                if not line:
                    continue
                low = line.lower()
                if low.startswith("جوائز:") or low.startswith("prizes:"):
                    prizes = line.split(":", 1)[-1].strip()
                else:
                    questions.append(line)
        reg_type = "FORM" if questions else "DIRECT"

        picker = EventHubTimePickerView(
            announce_channel=self.announce_channel,
            admin_channel=self.admin_channel,
            title=self.f_title.value.strip(),
            description=self.f_desc.value.strip(),
            capacity=capacity,
            reg_type=reg_type,
            questions=questions,
            prizes=prizes,
            duration_hours=duration_hours,
        )
        await interaction.response.send_message(
            content=picker.render_content(),
            view=picker,
            ephemeral=True,
        )


class BulkAttendanceModal(discord.ui.Modal, title="✅  الحضور — ألصق معرّفات الأعضاء"):
    ids_input = discord.ui.TextInput(
        label="معرّف كل عضو حاضر في سطر مستقل",
        style=discord.TextStyle.paragraph, max_length=2000,
        placeholder="123456789012345678\n987654321098765432",
    )

    def __init__(self, event_id: int) -> None:
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ids = [int(l) for l in self.ids_input.value.splitlines() if l.strip().isdigit()]
        if not ids:
            return await interaction.followup.send("⚠️  لم أجد أي معرّف صحيح.", ephemeral=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        now = datetime.now(timezone.utc).isoformat()
        all_reg = {
            int(r["user_id"])
            for r in (
                await client.table("event_participants").select("user_id")
                .eq("event_id", self.event_id).eq("status", "REGISTERED").execute()
            ).data or []
        }
        updated = 0
        for uid in ids:
            r = (
                await client.table("event_participants")
                .update({"attended": True, "attendance_by": interaction.user.id, "attendance_at": now})
                .eq("event_id", self.event_id).eq("user_id", uid).execute()
            ).data
            if r:
                updated += 1
        absent = all_reg - set(ids)
        suspended = 0
        for uid in absent:
            resp = await client.rpc("record_event_no_show", {
                "p_guild_id": interaction.guild.id, "p_event_id": self.event_id,
                "p_user_id": uid, "p_recorded_by": interaction.user.id,
            }).execute()
            d = (resp.data[0] if isinstance(resp.data, list) else resp.data) or {}
            if d.get("suspended"):
                suspended += 1
        await interaction.followup.send(
            f"✅  حضور: {updated}  |  غياب: {len(absent)}" +
            (f"\n🚫  إيقاف {suspended} عضو." if suspended else ""),
            ephemeral=True,
        )


class WinnersModal(discord.ui.Modal, title="🏆  إعلان الفائزين"):
    first  = discord.ui.TextInput(label="🥇  المركز الأول", max_length=100)
    second = discord.ui.TextInput(label="🥈  المركز الثاني", max_length=100, required=False)
    third  = discord.ui.TextInput(label="🥉  المركز الثالث", max_length=100, required=False)
    extra  = discord.ui.TextInput(label="مراكز أخرى (اختياري)", style=discord.TextStyle.paragraph, max_length=400, required=False)

    def __init__(self, event_id: int) -> None:
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        ev = (await client.table("events").select("title,channel_id").eq("id", self.event_id).limit(1).execute()).data
        if not ev:
            return await interaction.followup.send("الفعالية غير موجودة.", ephemeral=True)
        ch = interaction.client.get_channel(int(ev[0]["channel_id"]))  # type: ignore[attr-defined]
        if not isinstance(ch, discord.TextChannel):
            return await interaction.followup.send("قناة الفعالية غير متاحة.", ephemeral=True)
        embed = discord.Embed(title=f"🏆  نتائج · {ev[0]['title']}", color=discord.Color.gold())
        embed.add_field(name="🥇  المركز الأول", value=self.first.value, inline=False)
        if self.second.value:
            embed.add_field(name="🥈  المركز الثاني", value=self.second.value, inline=False)
        if self.third.value:
            embed.add_field(name="🥉  المركز الثالث", value=self.third.value, inline=False)
        if self.extra.value:
            embed.add_field(name="🏅  مراكز أخرى", value=self.extra.value, inline=False)
        embed.set_footer(text=f"أعلن من قبل {interaction.user}  •  AdSentry EventHub")
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send("🏆  تم إعلان الفائزين!", ephemeral=True)


class RejectModal(discord.ui.Modal, title="رفض طلب التسجيل"):
    reason = discord.ui.TextInput(
        label="سبب الرفض (اختياري)",
        style=discord.TextStyle.paragraph, max_length=400, required=False,
        placeholder="اكتب سبباً يُرسل للعضو، أو اتركه فارغاً",
    )

    def __init__(self, event_id: int, participant_id: int) -> None:
        super().__init__()
        self.event_id = event_id
        self.participant_id = participant_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
        res = (
            await client.table("event_participants")
            .update({"status": "CANCELLED", "reviewed_by": interaction.user.id})
            .eq("id", self.participant_id).eq("status", "PENDING").execute()
        ).data
        if not res:
            return await interaction.followup.send("الطلب مراجع مسبقاً.", ephemeral=True)
        p = res[0]
        ev = (await client.table("events").select("title").eq("id", self.event_id).limit(1).execute()).data
        title = ev[0]["title"] if ev else "الفعالية"
        try:
            u = await interaction.client.fetch_user(int(p["user_id"]))  # type: ignore[attr-defined]
            dm = discord.Embed(title="❌  تعذر قبول طلبك", description=f"طلب التسجيل في **{title}** تعذر قبوله.", color=discord.Color.red())
            if self.reason.value:
                dm.add_field(name="السبب", value=self.reason.value, inline=False)
            await u.send(embed=dm)
        except discord.HTTPException:
            pass
        if interaction.message and interaction.message.embeds:
            e = interaction.message.embeds[0]
            e.color = discord.Color.red()
            e.set_footer(text=f"❌  رُفض من {interaction.user}")
            await interaction.message.edit(embed=e, view=None)
        await interaction.followup.send("❌  تم الرفض وإشعار العضو.")


async def send_admin_event_notification(
    bot: commands.Bot,
    admin_channel_id: int,
    *,
    event_id: int,
    event_title: str,
    kind: str,  # "new_registration" | "event_closed" | "reminder"
    user: discord.User | None = None,
    registered: int = 0,
    capacity: int = 0,
    guild: discord.Guild | None = None,
) -> None:
    """Send a private staff notification to the admin channel."""
    channel = bot.get_channel(admin_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    if kind == "new_registration" and user:
        embed = discord.Embed(
            title="📥  تسجيل جديد في فعالية",
            description=f"سجّل **{user}** في فعالية **{event_title}** (#{event_id})",
            color=discord.Color.green(),
        )
        embed.add_field(name="المقاعد المشغولة", value=f"`{registered} / {capacity}`", inline=True)
        embed.add_field(name="المتبقي", value=f"`{capacity - registered}`", inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"AdSentry EventHub  •  #{event_id}")
    elif kind == "event_closed":
        embed = discord.Embed(
            title="🔒  أُغلق التسجيل في فعالية",
            description=f"**{event_title}** (#{event_id})\nإجمالي المسجلين: **{registered}**",
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text="AdSentry EventHub")
    elif kind == "reminder":
        embed = discord.Embed(
            title="⏰  تذكير: فعالية تبدأ قريبًا",
            description=f"**{event_title}** (#{event_id})\nالمسجلون: **{registered} / {capacity}**",
            color=discord.Color.gold(),
        )
        embed.set_footer(text="AdSentry EventHub")
    else:
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.warning("Could not send admin event notification to channel %s", admin_channel_id)


# ──────────────────────────────────────────────────────────────────────────────
# Views
# ──────────────────────────────────────────────────────────────────────────────

class RegisterEventView(discord.ui.View):
    def __init__(self, event_id: int) -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="✋  سجّل في الفعالية",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventhub:register:{event_id}",
        )
        button.callback = self._register  # type: ignore[method-assign]
        self.add_item(button)
        self.event_id = event_id

    async def _register(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم هذا الزر داخل السيرفر.", ephemeral=True); return
        try:
            client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
            response = await client.rpc("register_event_participant", {
                "p_event_id": self.event_id,
                "p_guild_id": interaction.guild.id,
                "p_user_id": interaction.user.id,
                "p_user_name": str(interaction.user),
            }).execute()
            data = response.data[0] if isinstance(response.data, list) else response.data
            result = data.get("result")
            role = None
            if result == "registered":
                role = await _grant_event_role(
                    interaction.guild,
                    interaction.user.id,
                    self.event_id,
                    client,
                )
            messages = {
                "registered": (
                    f"✅  تم تسجيلك! أُضيفت لك رتبة `{role.name}`."
                    if role else "✅  تم تسجيلك، لكن تعذرت إضافة رتبة الفعالية؛ أبلغ الإدارة."
                ),
                "full": "❌  اكتملت المقاعد.",
                "duplicate": "ℹ️  أنت مسجل بالفعل في هذه الفعالية.",
                "closed": "🔒  التسجيل مغلق أو انتهت الفعالية.",
                "not_found": "⚠️  الفعالية غير موجودة.",
            }
            await interaction.response.send_message(
                messages.get(result, "تعذر إتمام التسجيل."), ephemeral=True
            )
            if result == "registered":
                try:
                    event_row = (await client.table("events").select(
                        "title,capacity,admin_channel_id"
                    ).eq("id", self.event_id).limit(1).execute()).data
                    if event_row:
                        ev = event_row[0]
                        admin_ch_id = ev.get("admin_channel_id")
                        if admin_ch_id:
                            reg_count = (await client.table("event_participants").select(
                                "id", count="exact"
                            ).eq("event_id", self.event_id).execute()).count or 0
                            await send_admin_event_notification(
                                interaction.client,  # type: ignore[arg-type]
                                int(admin_ch_id),
                                event_id=self.event_id,
                                event_title=ev["title"],
                                kind="new_registration",
                                user=interaction.user,
                                registered=reg_count,
                                capacity=ev["capacity"],
                                guild=interaction.guild,
                            )
                    await _refresh_event_announcement(
                        interaction.client,  # type: ignore[arg-type]
                        client,
                        self.event_id,
                    )
                except Exception:
                    logger.exception("Admin notification after registration failed")
        except Exception:
            logger.exception("Event button registration failed")
            if not interaction.response.is_done():
                await interaction.response.send_message("حدث خطأ أثناء التسجيل.", ephemeral=True)


async def _refresh_event_announcement(
    bot: commands.Bot,
    client: object,
    event_id: int,
) -> None:
    """Update the public event embed to reflect the current seat count."""
    try:
        await _refresh_embed(bot, event_id)
        return
    except Exception:
        pass
    try:
        event_row = (await client.table("events").select(  # type: ignore[attr-defined]
            "title,description,starts_at,ends_at,capacity,channel_id,announcement_message_id,status,created_by,registration_type"
        ).eq("id", event_id).limit(1).execute()).data
        if not event_row:
            return
        ev = event_row[0]
        channel = bot.get_channel(int(ev["channel_id"]))
        if not isinstance(channel, discord.TextChannel) or not ev.get("announcement_message_id"):
            return
        registered = (await client.table("event_participants").select(  # type: ignore[attr-defined]
            "id", count="exact"
        ).eq("event_id", event_id).execute()).count or 0
        raw_start = ev.get("starts_at")
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00")) if isinstance(raw_start, str) else raw_start
        organizer = bot.get_user(int(ev["created_by"])) if ev.get("created_by") else None
        embed = build_event_embed(
            ev["title"], ev["description"], start,
            ev["capacity"], registered,
            event_id=event_id,
            organizer=organizer,
            status=ev.get("status", "OPEN"),
            ends_at=(
                datetime.fromisoformat(ev["ends_at"].replace("Z", "+00:00"))
                if isinstance(ev.get("ends_at"), str) else ev.get("ends_at")
            ),
        )
        message = await channel.fetch_message(int(ev["announcement_message_id"]))
        view = RegisterView(event_id, ev.get("registration_type", "DIRECT")) if ev.get("status") == "OPEN" else None
        await message.edit(embed=embed, view=view)
    except Exception:
        logger.warning("Could not refresh event announcement embed for event %s", event_id)


async def refresh_event_announcement(
    bot: commands.Bot,
    client: object,
    event_id: int,
) -> None:
    await _refresh_event_announcement(bot, client, event_id)


class EventHub(commands.GroupCog, group_name="event"):
    """AdSentry EventHub — complete events lifecycle management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._participant_roles_synced = False

    async def cog_load(self) -> None:
        self.reminder_sweeper.start()
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("events").select("id,registration_type").eq("status", "OPEN").execute()).data or []
            for r in rows:
                event_id = int(r["id"])
                self.bot.add_view(RegisterView(event_id, r.get("registration_type", "DIRECT")))
                self.bot.add_view(RegisterEventView(event_id))
                self.bot.add_view(AdminView(int(r["id"])))
        except Exception:
            logger.exception("Could not restore persistent event views")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._participant_roles_synced:
            return
        try:
            client = await self.bot.db.get_client()
            events = (
                await client.table("events")
                .select("id,guild_id,role_slot")
                .eq("status", "OPEN")
                .not_.is_("role_slot", "null")
                .execute()
            ).data or []
            expected_roles: dict[tuple[int, int], set[int]] = {}
            succeeded = True
            for event in events:
                guild = self.bot.get_guild(int(event["guild_id"]))
                if guild is None:
                    succeeded = False
                    continue
                role = await _event_role(guild, int(event["id"]), client)
                if role is None:
                    succeeded = False
                    continue
                participants = (
                    await client.table("event_participants")
                    .select("user_id")
                    .eq("event_id", int(event["id"]))
                    .eq("status", "REGISTERED")
                    .execute()
                ).data or []
                for participant in participants:
                    user_id = int(participant["user_id"])
                    expected_roles.setdefault((guild.id, user_id), set()).add(role.id)
                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            continue
                        except discord.HTTPException:
                            logger.exception("Could not load EventHub participant %s during role sync", user_id)
                            succeeded = False
                            continue
                    if role not in member.roles:
                        try:
                            await member.add_roles(role, reason=f"Restore EventHub event #{event['id']} role")
                        except discord.HTTPException:
                            logger.exception("Could not restore EventHub role for participant %s", user_id)
                            succeeded = False

            for (guild_id, user_id), role_ids in expected_roles.items():
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild else None
                if guild is None or member is None:
                    continue
                for legacy_name in ("Event", "🎉 Events"):
                    legacy_role = discord.utils.get(guild.roles, name=legacy_name)
                    if legacy_role and legacy_role.id not in role_ids and legacy_role in member.roles:
                        try:
                            await member.remove_roles(
                                legacy_role,
                                reason="Replace shared EventHub role with event-specific roles",
                            )
                        except discord.HTTPException:
                            logger.exception("Could not remove legacy EventHub role from participant %s", user_id)
                            succeeded = False

            cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            recently_closed = (
                await client.table("events")
                .select("id,guild_id")
                .eq("status", "CLOSED")
                .not_.is_("closed_at", "null")
                .gte("closed_at", cutoff)
                .limit(100)
                .execute()
            ).data or []
            for event in recently_closed:
                guild = self.bot.get_guild(int(event["guild_id"]))
                if guild:
                    await _strip_role_all(guild, int(event["id"]), client)
                await _refresh_event_announcement(self.bot, client, int(event["id"]))
            self._participant_roles_synced = succeeded
        except Exception:
            logger.exception("Could not synchronize participant roles for open EventHub events")

    def cog_unload(self) -> None:
        self.reminder_sweeper.cancel()

    @tasks.loop(seconds=30)
    async def reminder_sweeper(self) -> None:
        try:
            client = await self.bot.db.get_client()
            now = datetime.now(timezone.utc).isoformat()
            # Auto-close events when their configured end time has passed.
            expired = (await client.table("events").update(
                {"status": "CLOSED", "closed_at": now}
            ).eq("status", "OPEN").lte("ends_at", now).execute()).data or []
            # Update embeds and notify admin for auto-closed events
            for ev in expired:
                try:
                    count = (await client.table("event_participants").select(
                        "id", count="exact"
                    ).eq("event_id", ev["id"]).execute()).count or 0
                    guild = self.bot.get_guild(int(ev["guild_id"]))
                    if guild:
                        await _strip_role_all(guild, int(ev["id"]), client)
                    await _refresh_event_announcement(self.bot, client, int(ev["id"]))
                    if ev.get("admin_channel_id"):
                        await send_admin_event_notification(
                            self.bot, int(ev["admin_channel_id"]),
                            event_id=int(ev["id"]), event_title=ev["title"],
                            kind="event_closed", registered=count, capacity=ev["capacity"],
                        )
                except Exception:
                    logger.exception("Could not finalize auto-closed event %s", ev.get("id"))
            # Send reminders
            rows = (await client.table("events").select("*").eq("status", "OPEN")
                    .not_.is_("reminder_at", "null").lte("reminder_at", now)
                    .is_("reminded_at", "null").execute()).data or []
            for event in rows:
                # Claim the reminder atomically before sending to prevent duplicates after reconnects.
                claimed = await client.table("events").update(
                    {"reminded_at": now}
                ).eq("id", event["id"]).is_("reminded_at", "null").execute()
                if not claimed.data:
                    continue
                start = datetime.fromisoformat(event["starts_at"].replace("Z", "+00:00"))
                reg_count = (await client.table("event_participants").select(
                    "id", count="exact"
                ).eq("event_id", event["id"]).execute()).count or 0
                # Public reminder in event channel
                channel = self.bot.get_channel(int(event["channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    remind_embed = discord.Embed(
                        title=f"⏰  تذكير · {event['title']}",
                        description=f"تبدأ الفعالية <t:{int(start.timestamp())}:R>!\nالمقاعد: `{reg_count} / {event['capacity']}`",
                        color=discord.Color.gold(),
                    )
                    remind_embed.set_footer(text=f"رقم الفعالية: #{event['id']}  •  AdSentry EventHub")
                    await channel.send(embed=remind_embed, allowed_mentions=discord.AllowedMentions.none())
                # Admin reminder notification
                if event.get("admin_channel_id"):
                    await send_admin_event_notification(
                        self.bot, int(event["admin_channel_id"]),
                        event_id=int(event["id"]), event_title=event["title"],
                        kind="reminder", registered=reg_count, capacity=event["capacity"],
                    )
        except Exception:
            logger.exception("Event reminder sweep failed")

    @reminder_sweeper.before_loop
    async def before_reminder_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="create", description="إنشاء فعالية — يفتح نموذجاً سهلاً")
    @app_commands.describe(
        announce_channel="القناة التي ينشر فيها إعلان الفعالية",
        admin_channel="قناة إدارة الفعالية الخاصة للمشرفين (اختياري)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        announce_channel: discord.TextChannel,
        admin_channel: discord.TextChannel | None = None,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        if not announce_channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message("❌  البوت لا يستطيع الإرسال في القناة المختارة.", ephemeral=True)
        try:
            client = await self.bot.db.get_client()
            open_count = (
                await client.table("events")
                .select("id", count="exact")
                .eq("guild_id", interaction.guild.id)
                .eq("status", "OPEN")
                .execute()
            ).count or 0
        except Exception:
            logger.exception("Could not check the open EventHub count for guild %s", interaction.guild.id)
            return await interaction.response.send_message(
                "تعذر التحقق من عدد الفعاليات المفتوحة. تحقق من قاعدة البيانات وترحيل EventHub.",
                ephemeral=True,
            )
        if open_count >= len(EVENT_ROLE_NAMES):
            return await interaction.response.send_message(
                "وصل السيرفر إلى الحد الأقصى: توجد خمس فعاليات/مسابقات مفتوحة بالفعل. أغلق واحدة قبل إنشاء غيرها.",
                ephemeral=True,
            )
        await interaction.response.send_modal(EventCreateModal(announce_channel, admin_channel))

    @app_commands.command(name="edit", description="تعديل موعد أو سعة أو نص فعالية أنشأتها")
    async def edit_event(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True)
        client = await self.bot.db.get_client()
        event = await _get_manageable_event(interaction, event_id, client)
        if not event:
            return await interaction.response.send_message(
                "تعديل الفعالية متاح لمنشئها أو للإدارة فقط، وتأكد من رقم الفعالية.",
                ephemeral=True,
            )
        if event["status"] != "OPEN":
            return await interaction.response.send_message("لا يمكن تعديل فعالية مغلقة أو ملغاة.", ephemeral=True)
        await interaction.response.send_modal(EventEditModal(event))

    @app_commands.command(name="cancel", description="إلغاء فعالية وإشعار المشاركين في قناة إعلانها")
    async def cancel_event(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True)
        client = await self.bot.db.get_client()
        event = await _get_manageable_event(interaction, event_id, client)
        if not event:
            return await interaction.response.send_message(
                "إلغاء الفعالية متاح لمنشئها أو للإدارة فقط، وتأكد من رقم الفعالية.",
                ephemeral=True,
            )
        if event["status"] != "OPEN":
            return await interaction.response.send_message("الفعالية مغلقة أو ملغاة مسبقًا.", ephemeral=True)
        await interaction.response.send_modal(EventCancelModal(event))




    @app_commands.command(name="close", description="إغلاق التسجيل في فعالية")
    async def close(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        client = await self.bot.db.get_client()
        now = datetime.now(timezone.utc).isoformat()
        res = await client.table("events").update(
            {"status": "CLOSED", "closed_at": now}
        ).eq("id", event_id).eq("guild_id", interaction.guild.id).eq("status", "OPEN").execute()
        if not res.data:
            await interaction.response.send_message("⚠️  الفعالية غير موجودة أو التسجيل مغلق مسبقًا.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ev = res.data[0]
        reg_count = (await client.table("event_participants").select(
            "id", count="exact"
        ).eq("event_id", event_id).execute()).count or 0
        await _strip_role_all(interaction.guild, event_id, client)
        await _refresh_event_announcement(self.bot, client, event_id)
        if ev.get("admin_channel_id"):
            await send_admin_event_notification(
                self.bot, int(ev["admin_channel_id"]),
                event_id=event_id, event_title=ev["title"],
                kind="event_closed", registered=reg_count, capacity=ev["capacity"],
            )
        await interaction.followup.send("🔒  تم إغلاق التسجيل في الفعالية.", ephemeral=True)

    @app_commands.command(name="list", description="عرض الفعاليات المفتوحة")
    async def list_events(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.bot.db.get_client()
        rows = (
            await client.table("events").select("id,title,starts_at,capacity")
            .eq("guild_id", interaction.guild.id).eq("status", "OPEN")
            .order("starts_at").limit(10).execute()
        ).data or []
        if not rows:
            return await interaction.followup.send("لا توجد فعاليات مفتوحة حالياً.", ephemeral=True)
        embed = discord.Embed(title="🎪  الفعاليات القادمة", color=discord.Color.blurple())
        for ev in rows:
            count = (
                await client.table("event_participants").select("id", count="exact")
                .eq("event_id", ev["id"]).eq("status", "REGISTERED").execute()
            ).count or 0
            ts = int(datetime.fromisoformat(ev["starts_at"].replace("Z", "+00:00")).timestamp())
            bar = _seats_bar(count, ev["capacity"])
            embed.add_field(
                name=f"#{ev['id']}  ·  {ev['title']}",
                value=f"<t:{ts}:F>\n`{count}/{ev['capacity']}` {bar}",
                inline=False,
            )
        embed.set_footer(text="AdSentry EventHub")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="strikes", description="عرض سجل غيابات عضو")
    async def strikes(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.bot.db.get_client()
        count = (
            await client.table("event_strikes").select("id", count="exact")
            .eq("guild_id", interaction.guild.id).eq("user_id", member.id).execute()
        ).count or 0
        sus = (
            await client.table("event_suspensions").select("suspended_until")
            .eq("guild_id", interaction.guild.id).eq("user_id", member.id).execute()
        ).data
        embed = discord.Embed(title=f"📊  سجل غيابات  ·  {member}", color=discord.Color.orange())
        embed.add_field(name="عدد الغيابات", value=f"{count} مرة", inline=True)
        if sus and sus[0]:
            try:
                ts = int(datetime.fromisoformat(sus[0]["suspended_until"].replace("Z", "+00:00")).timestamp())
                active = datetime.fromisoformat(sus[0]["suspended_until"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
                txt = f"🚫  موقوف حتى <t:{ts}:F>" if active else "✅  انتهى الإيقاف"
            except Exception:
                txt = "—"
            embed.add_field(name="الحالة", value=txt, inline=True)
        else:
            embed.add_field(name="الحالة", value="✅  لا يوجد إيقاف", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventHub(bot))
