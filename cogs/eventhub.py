"""EventHub: professional events management — UX-optimised for staff and members."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont
from discord import app_commands
from discord.ext import commands, tasks

from cogs.moderation import is_admin, is_staff

logger = logging.getLogger("AdSentry.EventHub")

EVENT_ROLE_NAMES = ("Event", "Event 2", "Event 3", "Event 4", "Event 5")
_EVENT_ROLE_LOCKS: dict[int, asyncio.Lock] = {}
DRAND_API_BASE = "https://api.drand.sh"
DRAND_DOMAIN = b"AdSentry-EventHub-Raffle-v1"
WHEEL_COLORS = ("#5865F2", "#EB459E", "#57F287", "#FEE75C", "#ED4245", "#9B59B6")


def _raffle_entrants_hash(entrants: list[dict[str, Any]]) -> str:
    canonical = [
        {
            "user_id": int(entrant["user_id"]),
            "user_name": str(entrant["user_name"]),
        }
        for entrant in sorted(entrants, key=lambda row: int(row["user_id"]))
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_raffle_csv_text(value: Any) -> str:
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _raffle_event_is_ready(event: dict[str, Any]) -> bool:
    status = event.get("status")
    return status == "CLOSED" or (
        status == "OPEN" and event.get("registration_open") is False
    )


def _raffle_wheel_image(entrants: list[dict[str, Any]], pointer_index: int = 0) -> io.BytesIO:
    if not entrants:
        raise ValueError("Cannot render a raffle wheel without entrants.")
    size = 900
    center = size // 2
    radius = 350
    image = Image.new("RGB", (size, size), "#202225")
    draw = ImageDraw.Draw(image)
    font_candidates = (
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabicUI-Regular.ttf",
        "DejaVuSans.ttf",
    )
    font_path = next((path for path in font_candidates if Path(path).exists()), None)
    font_size = max(11, min(25, int(520 / max(20, len(entrants) * 0.75))))
    font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()

    slice_size = 360 / len(entrants)
    for index, entrant in enumerate(entrants):
        start = -90 + index * slice_size
        end = -90 + (index + 1) * slice_size
        draw.pieslice(
            (center - radius, center - radius, center + radius, center + radius),
            start=start,
            end=end,
            fill=WHEEL_COLORS[index % len(WHEEL_COLORS)],
            outline="#FFFFFF",
            width=3,
        )
        angle = math.radians(start + slice_size / 2)
        label_radius = radius * 0.67
        x = center + math.cos(angle) * label_radius
        y = center + math.sin(angle) * label_radius
        name = str(entrant["user_name"]).replace("\n", " ").strip()
        max_chars = max(8, min(22, int(360 / max(1, len(entrants)))))
        if len(name) > max_chars:
            name = name[:max_chars - 1] + "…"
        label_box = draw.textbbox((0, 0), name, font=font)
        text_width = label_box[2] - label_box[0]
        text_height = label_box[3] - label_box[1]
        draw.rounded_rectangle(
            (x - text_width / 2 - 7, y - text_height / 2 - 5,
             x + text_width / 2 + 7, y + text_height / 2 + 5),
            radius=6,
            fill="#202225",
        )
        draw.text((x, y), name, fill="#FFFFFF", font=font, anchor="mm")

    selected_index = pointer_index % len(entrants)
    pointer_angle = math.radians(-90 + (selected_index + 0.5) * slice_size)
    tip_radius = radius - 5
    base_radius = radius + 54
    half_width = 28
    tip = (
        center + math.cos(pointer_angle) * tip_radius,
        center + math.sin(pointer_angle) * tip_radius,
    )
    base_center = (
        center + math.cos(pointer_angle) * base_radius,
        center + math.sin(pointer_angle) * base_radius,
    )
    perpendicular = pointer_angle + math.pi / 2
    base_left = (
        base_center[0] + math.cos(perpendicular) * half_width,
        base_center[1] + math.sin(perpendicular) * half_width,
    )
    base_right = (
        base_center[0] - math.cos(perpendicular) * half_width,
        base_center[1] - math.sin(perpendicular) * half_width,
    )
    draw.polygon([tip, base_left, base_right], fill="#F1C40F", outline="#111111")
    draw.ellipse(
        (center - 46, center - 46, center + 46, center + 46),
        fill="#FFFFFF",
        outline="#202225",
        width=5,
    )
    draw.ellipse((center - 12, center - 12, center + 12, center + 12), fill="#202225")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _can_spin_raffle(interaction: discord.Interaction, raffle: dict[str, Any]) -> bool:
    return bool(
        interaction.guild
        and is_staff(interaction.user)
        and (
            is_admin(interaction.user)
            or interaction.user.id == int(raffle["created_by"])
        )
    )


class RaffleSpinView(discord.ui.View):
    def __init__(self, cog: "EventHub", raffle_id: int, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.raffle_id = raffle_id
        button = discord.ui.Button(
            label="تدوير الدولاب",
            emoji="🎡",
            style=discord.ButtonStyle.success,
            custom_id=f"adsentry:raffle:spin:{raffle_id}",
            disabled=disabled,
        )
        button.callback = self.spin
        self.add_item(button)

    async def spin(self, interaction: discord.Interaction) -> None:
        await self.cog.spin_raffle(interaction, self.raffle_id, interaction.message)


class RaffleEntrantAddView(discord.ui.View):
    def __init__(self, cog: "EventHub", raffle_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.raffle_id = raffle_id
        select = discord.ui.UserSelect(
            placeholder="اختر المؤهلين (يمكن اختيار عدة أعضاء)",
            min_values=1,
            max_values=25,
        )
        select.callback = self.add_selected
        self.select = select
        self.add_item(select)

    async def add_selected(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raffle = await self.cog.bot.db.get_event_raffle(self.raffle_id, interaction.guild.id)
            if not raffle or raffle.get("status") != "OPEN":
                return await interaction.followup.send("السحب غير موجود أو لم يعد مفتوحًا لإضافة المؤهلين.", ephemeral=True)
            added: list[str] = []
            already_present: list[str] = []
            skipped: list[str] = []
            for user in self.select.values:
                if not isinstance(user, discord.Member) or user.bot:
                    skipped.append(f"<@{user.id}> (غير عضو أو حساب بوت)")
                    continue
                member = user
                try:
                    result = await self.cog.bot.db.manage_event_raffle_entry(
                        raffle_id=self.raffle_id,
                        guild_id=interaction.guild.id,
                        actor_id=interaction.user.id,
                        user_id=member.id,
                        user_name=member.display_name,
                        action="ADD",
                    )
                    if result.get("already_present"):
                        already_present.append(member.mention)
                    else:
                        added.append(member.mention)
                except Exception:
                    skipped.append(member.mention)
                    logger.info(
                        "Could not add selected entrant %s to raffle %s",
                        member.id,
                        self.raffle_id,
                        exc_info=True,
                    )
        except Exception:
            logger.exception("Could not process selected entrants for raffle %s", self.raffle_id)
            return await interaction.followup.send("تعذر تحميل السحب أو تحديث قائمة المؤهلين.", ephemeral=True)

        message = f"تمت إضافة {len(added)} مؤهل جديد إلى السحب `#{self.raffle_id}`."
        if already_present:
            message += f"\nكان {len(already_present)} موجودين مسبقًا ولم تتم إضافتهم مرة أخرى: "
            message += ", ".join(already_present[:10])
        if skipped:
            message += "\nتعذر تسجيل: " + ", ".join(skipped[:10])
        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def create_raffle_from_closed_event(
    bot: commands.Bot,
    guild: discord.Guild,
    actor: discord.Member,
    event_id: int,
    channel: discord.TextChannel,
    winner_count: int = 1,
) -> dict[str, Any]:
    if not is_staff(actor):
        raise PermissionError("Only staff can create raffles.")
    if channel.guild.id != guild.id:
        raise ValueError("The raffle channel must belong to this server.")
    bot_member = guild.me
    if bot_member is None:
        raise RuntimeError("The bot member could not be resolved.")
    permissions = channel.permissions_for(bot_member)
    if not permissions.send_messages or not permissions.embed_links or not permissions.attach_files:
        raise PermissionError("The bot needs message, embed, and attachment permissions in the selected channel.")

    client = await bot.db.get_client()
    events = (
        await client.table("events")
        .select("id,title,status,registration_open")
        .eq("id", event_id)
        .eq("guild_id", guild.id)
        .limit(1)
        .execute()
    ).data or []
    if not events or not _raffle_event_is_ready(events[0]):
        raise ValueError("The event must be ended or have registration closed before creating a raffle.")
    event = events[0]
    raffle = await bot.db.create_event_raffle(
        guild_id=guild.id,
        created_by=actor.id,
        title=str(event["title"])[:100],
        channel_id=channel.id,
        winner_count=winner_count,
        event_id=event_id,
    )
    embed = discord.Embed(
        title=f"🎟️ سحب الفعالية #{event_id} · {event['title']}",
        description=(
            f"عدد الفائزين: `{winner_count}`\n"
            "أُغلق التسجيل أو انتهت الفعالية. ستُدرج الإدارة المؤهلين يدويًا، ثم تثبّت القائمة وتنشر بصمتها "
            "وسجل التغييرات قبل السحب. لا تختار العجلة الفائز؛ النتيجة تحسمها عشوائية عامة قابلة للتحقق."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"السحب #{raffle['id']} · اضغط زر إدراج المؤهلين في لوحة الإدارة")
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.exception("Raffle %s was created but its setup announcement could not be sent", raffle["id"])
        raffle["_announcement_sent"] = False
    else:
        raffle["_announcement_sent"] = True
    return raffle


def _raffle_target_round(locked_at: str, info: dict[str, Any]) -> int:
    locked = datetime.fromisoformat(locked_at.replace("Z", "+00:00"))
    if locked.tzinfo is None:
        locked = locked.replace(tzinfo=timezone.utc)
    period = int(info["period"])
    genesis_time = int(info["genesis_time"])
    if period <= 0:
        raise ValueError("Invalid drand period.")
    return max(1, int((locked.timestamp() - genesis_time) // period) + 4)


def _select_raffle_winners(
    randomness: str,
    raffle_id: int,
    entrants: list[dict[str, Any]],
    winner_count: int,
    entrants_hash: str | None = None,
) -> list[int]:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", randomness):
        raise ValueError("Invalid drand randomness value.")
    if not entrants or not 1 <= winner_count <= len(entrants):
        raise ValueError("Winner count must be between one and the entrant count.")

    ordered = sorted(entrants, key=lambda row: int(row["user_id"]))
    digest = entrants_hash or _raffle_entrants_hash(ordered)
    random_bytes = bytes.fromhex(randomness)
    selected: list[int] = []
    remaining = [int(entrant["user_id"]) for entrant in ordered]
    draw_index = 0

    while len(selected) < winner_count:
        candidate_count = len(remaining)
        space = 1 << 256
        acceptance_limit = space - (space % candidate_count)
        nonce = 0
        while True:
            message = (
                f"{raffle_id}|{digest}|{draw_index}|{nonce}".encode("ascii")
            )
            value = int.from_bytes(
                hashlib.sha256(DRAND_DOMAIN + random_bytes + message).digest(),
                "big",
            )
            if value < acceptance_limit:
                break
            nonce += 1

        selected.append(remaining.pop(value % candidate_count))
        draw_index += 1

    return selected


async def _fetch_drand_json(path: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{DRAND_API_BASE}{path}") as response:
            response.raise_for_status()
            payload = await response.json()
    if not isinstance(payload, dict):
        raise ValueError("Invalid response from the drand randomness beacon.")
    return payload


async def _fetch_drand_info() -> dict[str, Any]:
    info = await _fetch_drand_json("/info")
    chain_hash = str(info.get("hash") or "")
    if (
        not re.fullmatch(r"[0-9a-fA-F]{64}", chain_hash)
        or int(info.get("period", 0)) <= 0
        or int(info.get("genesis_time", 0)) <= 0
    ):
        raise ValueError("The drand chain returned invalid configuration.")
    return info


async def _fetch_drand_beacon(round_number: int) -> dict[str, Any]:
    beacon = await _fetch_drand_json(f"/public/{round_number}")
    randomness = str(beacon.get("randomness") or "")
    signature = str(beacon.get("signature") or "")
    if int(beacon.get("round", 0)) != round_number:
        raise ValueError("The drand response did not match the committed round.")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", randomness):
        raise ValueError("The drand round has no valid randomness value.")
    if not re.fullmatch(r"[0-9a-fA-F]{64,512}", signature):
        raise ValueError("The drand round has no valid signature.")
    if hashlib.sha256(bytes.fromhex(signature)).hexdigest() != randomness.lower():
        raise ValueError("The drand signature does not match its randomness.")
    return beacon


def _event_role_name(slot: int) -> str:
    if not 1 <= slot <= len(EVENT_ROLE_NAMES):
        raise ValueError(f"Event role slot must be between 1 and {len(EVENT_ROLE_NAMES)}.")
    return EVENT_ROLE_NAMES[slot - 1]


def _available_event_role_slots(open_events: list[dict]) -> list[int]:
    occupied = {
        int(event["role_slot"])
        for event in open_events
        if event.get("role_slot") is not None
    }
    unassigned_count = sum(event.get("role_slot") is None for event in open_events)
    for slot in range(1, len(EVENT_ROLE_NAMES) + 1):
        if unassigned_count == 0:
            break
        if slot not in occupied:
            occupied.add(slot)
            unassigned_count -= 1
    return [slot for slot in range(1, len(EVENT_ROLE_NAMES) + 1) if slot not in occupied]


async def get_available_event_role_slots(client: Any, guild_id: int) -> list[int]:
    rows = (
        await client.table("events")
        .select("role_slot")
        .eq("guild_id", guild_id)
        .eq("status", "OPEN")
        .execute()
    ).data or []
    return _available_event_role_slots(rows)


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


def _parse_event_duration(text: str) -> timedelta:
    value = text.strip().lower()
    if re.fullmatch(r"\d+", value):
        value += "h"
    match = re.fullmatch(r"(\d+)\s*([mhdw])", value)
    if not match:
        raise ValueError("أدخل المدة مثل 30m أو 1h أو 1d أو 1w، وبحد أقصى 4 أسابيع.")

    amount = int(match.group(1))
    unit_minutes = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    duration_minutes = amount * unit_minutes[match.group(2)]
    if not 1 <= duration_minutes <= 4 * 7 * 24 * 60:
        raise ValueError("مدة الفعالية يجب أن تكون بين دقيقة واحدة و4 أسابيع.")
    return timedelta(minutes=duration_minutes)


def _format_event_duration(duration: timedelta) -> str:
    total_minutes = int(duration.total_seconds() // 60)
    for unit, unit_minutes in (("w", 10080), ("d", 1440), ("h", 60)):
        if total_minutes % unit_minutes == 0:
            return f"{total_minutes // unit_minutes}{unit}"
    return f"{total_minutes}m"


def _event_end_datetime(starts_at: datetime, duration: timedelta) -> datetime:
    if duration < timedelta(minutes=1) or duration > timedelta(weeks=4):
        raise ValueError("مدة الفعالية يجب أن تكون بين دقيقة واحدة و4 أسابيع.")
    return starts_at + duration


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
    registration_open = status == "OPEN" and event.get("registration_open", True)
    registration_state = "مفتوح" if registration_open else "مغلق"
    if status == "OPEN" and not registration_open:
        registration_state += " — الفعالية مستمرة"
    embed.add_field(name="📝  التسجيل", value=registration_state, inline=True)
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
    if event.get("registration_open") is False:
        embed.add_field(name="📝  حالة التسجيل", value="مغلق — الفعالية مستمرة", inline=True)
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
                view = RegisterView(
                    event_id,
                    ev.get("registration_type", "DIRECT"),
                    ev.get("registration_open", True),
                ) if ev["status"] == "OPEN" else None
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

    def __init__(
        self,
        event_id: int,
        reg_type: str = "DIRECT",
        registration_open: bool = True,
    ) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        label = "📝  أدخل معلوماتك للتسجيل" if reg_type == "FORM" else "🎟️  سجّل في الفعالية"
        reg = discord.ui.Button(
            label=label, style=discord.ButtonStyle.success,
            custom_id=f"eh:reg:{event_id}",
            disabled=not registration_open,
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
        if not ev.get("registration_open", True):
            return await interaction.response.send_message(
                "🔒  أوقف المنظم التسجيلات الجديدة لهذه الفعالية.",
                ephemeral=True,
            )
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
            "registration_closed": "🔒  أوقف المنظم التسجيلات الجديدة لهذه الفعالية.",
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
            "registration_closed": "🔒  أوقف المنظم التسجيلات الجديدة لهذه الفعالية.",
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
        duration = ends_at - starts_at
        self.duration_input = discord.ui.TextInput(
            label="المدة (مثل 30m أو 1h أو 1d أو 1w)",
            default=_format_event_duration(duration),
            min_length=1,
            max_length=5,
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
            duration = _parse_event_duration(self.duration_input.value)
            ends_at = _event_end_datetime(starts_at, duration)
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
        res = (
            await client.table("events").update({"registration_open": False})
            .eq("id", self.event_id).eq("status", "OPEN")
            .eq("registration_open", True).execute()
        ).data
        if not res:
            return await interaction.followup.send("التسجيل مغلق مسبقاً أو انتهت الفعالية.", ephemeral=True)
        await _refresh_embed(interaction.client, self.event_id)  # type: ignore[arg-type]
        await interaction.followup.send(
            "🔒  تم إيقاف التسجيلات الجديدة فقط. الفعالية ما زالت مفتوحة، وستبقى رتب المشاركين حتى موعد انتهائها.",
            ephemeral=True,
        )

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
        duration: timedelta,
        available_role_slots: list[int],
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
        self.duration = duration
        self.available_role_slots = available_role_slots
        self.selected_role_slot: int | None = None

        self.tz = timezone(timedelta(hours=3))
        now_local = datetime.now(self.tz)

        tomorrow = (now_local + timedelta(days=1)).date()
        self.selected_date = tomorrow.isoformat()
        self.selected_time = "20:00"

        self._build_date_select(now_local)
        self._build_time_select()
        self._build_preset_select()
        self._build_role_select()

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

    def _build_role_select(self) -> None:
        options = [
            discord.SelectOption(
                label=f"{_event_role_name(slot)} · رتبة الفعالية",
                value=str(slot),
                default=(slot == self.selected_role_slot),
            )
            for slot in self.available_role_slots
        ]
        self.role_select = discord.ui.Select(
            placeholder="🏷️ اختر رتبة هذه الفعالية",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )
        self.role_select.callback = self._on_role_change
        self.add_item(self.role_select)

    def get_selected_datetime(self) -> datetime:
        d = datetime.fromisoformat(self.selected_date).date()
        h, m = map(int, self.selected_time.split(":"))
        local_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
        return local_dt.astimezone(timezone.utc)

    def get_end_datetime(self) -> datetime:
        return _event_end_datetime(self.get_selected_datetime(), self.duration)

    def render_content(self) -> str:
        dt_utc = self.get_selected_datetime()
        ts = int(dt_utc.timestamp())
        end_ts = int(self.get_end_datetime().timestamp())
        role_name = _event_role_name(self.selected_role_slot) if self.selected_role_slot else "لم يتم اختيارها بعد"
        admin_info = f" · إشعارات الإدارة: {self.admin_channel.mention}" if self.admin_channel else ""
        return (
            f"🎪 **تحديد موعد الفعالية: {self.title}**\n\n"
            f"📅 **البداية:** <t:{ts}:F> (<t:{ts}:R>)\n"
            f"🕒 **النهاية:** <t:{end_ts}:F> · المدة `{_format_event_duration(self.duration)}`\n"
            f"🏷️ **رتبة المشاركين:** `{role_name}`\n"
            f"👥 **المقاعد:** `{self.capacity}`  ·  📢 **القناة:** {self.announce_channel.mention}{admin_info}\n\n"
            "👇 **حدد الموعد والرتبة المناسبة من القوائم أدناه، ثم اضغط على زر التأكيد:**"
        )

    def _refresh_defaults(self) -> None:
        for opt in self.date_select.options:
            opt.default = (opt.value == self.selected_date)
        for opt in self.time_select.options:
            opt.default = (opt.value == self.selected_time)
        for opt in self.role_select.options:
            opt.default = (opt.value == str(self.selected_role_slot))

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

    async def _on_role_change(self, interaction: discord.Interaction) -> None:
        self.selected_role_slot = int(self.role_select.values[0])
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    @discord.ui.button(label="تأكيد ونشر الفعالية 🚀", style=discord.ButtonStyle.success, row=4)
    async def confirm_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        start_utc = self.get_selected_datetime()
        end_utc = self.get_end_datetime()
        if start_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message("❌ الموعد المحدد أصبح في الماضي؛ يرجى اختيار موعد قادم.", ephemeral=True)
            return

        if not interaction.guild:
            await interaction.response.send_message("يجب إنشاء الفعالية داخل السيرفر.", ephemeral=True)
            return
        if self.selected_role_slot not in self.available_role_slots:
            await interaction.response.send_message("اختر رتبة متاحة لهذه الفعالية قبل التأكيد.", ephemeral=True)
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
                "p_role_slot": self.selected_role_slot,
            }).execute()
            result = response.data[0] if isinstance(response.data, list) else response.data
            if not isinstance(result, dict):
                raise RuntimeError("Event creation returned an invalid database response.")
            if not result.get("created"):
                if result.get("reason") == "limit":
                    await interaction.followup.send(
                        "وصل السيرفر إلى الحد الأقصى: توجد خمس فعاليات/مسابقات مفتوحة بالفعل. ألغِ فعالية أو انتظر انتهاءها قبل إنشاء غيرها.",
                        ephemeral=True,
                    )
                    return
                if result.get("reason") == "slot_taken":
                    await interaction.followup.send(
                        "هذه الرتبة حُجزت لفعالية أخرى أثناء تجهيز طلبك. أعد فتح الإنشاء واختر رتبة متاحة.",
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
                "تعذر إنشاء المسابقة. تحقق من تطبيق ترحيل `eventhub_manual_role_slots_migration.sql` ثم حاول مجددًا.",
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

    @discord.ui.button(label="إلغاء ❌", style=discord.ButtonStyle.secondary, row=4)
    async def cancel_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="❌ تم إلغاء إنشاء الفعالية.", view=None)


class EventCreateModal(discord.ui.Modal, title="🎪  إنشاء فعالية جديدة"):
    """Staff modal — fills metadata and opens interactive DateTimePicker."""

    f_title    = discord.ui.TextInput(label="اسم الفعالية", max_length=100, placeholder="مثال: بطولة فوت سيك")
    f_desc     = discord.ui.TextInput(label="الوصف", style=discord.TextStyle.paragraph, max_length=1500, placeholder="تفاصيل الفعالية...")
    f_capacity = discord.ui.TextInput(label="عدد المقاعد", max_length=4, default="25", placeholder="25")
    f_duration = discord.ui.TextInput(
        label="المدة (مثل 30m أو 1h أو 1d أو 1w)",
        max_length=5,
        default="2h",
        placeholder="مثال: 30m أو 1d",
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
            duration = _parse_event_duration(self.f_duration.value)
        except ValueError as exc:
            return await interaction.response.send_message(f"❌  {exc}", ephemeral=True)

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

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
            available_role_slots = await get_available_event_role_slots(client, interaction.guild.id)
        except Exception:
            logger.exception("Could not load available EventHub roles for guild %s", interaction.guild.id)
            await interaction.edit_original_response(
                content="تعذر تحميل رتب الفعاليات المتاحة. تحقق من قاعدة البيانات وحاول مجددًا.",
                view=None,
            )
            return
        if not available_role_slots:
            await interaction.edit_original_response(
                content="كل رتب الفعاليات الخمس مستخدمة حاليًا. ألغِ فعالية أو انتظر انتهاءها قبل إنشاء أخرى.",
                view=None,
            )
            return

        picker = EventHubTimePickerView(
            announce_channel=self.announce_channel,
            admin_channel=self.admin_channel,
            title=self.f_title.value.strip(),
            description=self.f_desc.value.strip(),
            capacity=capacity,
            reg_type=reg_type,
            questions=questions,
            prizes=prizes,
            duration=duration,
            available_role_slots=available_role_slots,
        )
        await interaction.edit_original_response(
            content=picker.render_content(),
            view=picker,
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
                "registration_closed": "🔒  أوقف المنظم التسجيلات الجديدة لهذه الفعالية.",
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
            "title,description,starts_at,ends_at,capacity,channel_id,announcement_message_id,status,created_by,registration_type,registration_open"
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
        view = RegisterView(
            event_id,
            ev.get("registration_type", "DIRECT"),
            ev.get("registration_open", True),
        ) if ev.get("status") == "OPEN" else None
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

    raffle = app_commands.Group(name="raffle", description="إدارة مسابقات السحب الشفاف")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._participant_roles_synced = False
        self._raffle_draw_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        if not self.reminder_sweeper.is_running():
            self.reminder_sweeper.start()
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("events").select("id,registration_type,registration_open").eq("status", "OPEN").execute()).data or []
            for r in rows:
                event_id = int(r["id"])
                self.bot.add_view(RegisterView(
                    event_id,
                    r.get("registration_type", "DIRECT"),
                    r.get("registration_open", True),
                ))
                self.bot.add_view(RegisterEventView(event_id))
                self.bot.add_view(AdminView(int(r["id"])))
            locked_raffles = (
                await client.table("event_raffles")
                .select("id,spin_message_id")
                .eq("status", "LOCKED")
                .not_.is_("spin_message_id", "null")
                .execute()
            ).data or []
            for raffle in locked_raffles:
                self.bot.add_view(
                    RaffleSpinView(self, int(raffle["id"])),
                    message_id=int(raffle["spin_message_id"]),
                )
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
                "وصل السيرفر إلى الحد الأقصى: توجد خمس فعاليات/مسابقات مفتوحة بالفعل. ألغِ فعالية أو انتظر انتهاءها قبل إنشاء غيرها.",
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.bot.db.get_client()
        res = await client.table("events").update(
            {"registration_open": False}
        ).eq("id", event_id).eq("guild_id", interaction.guild.id).eq("status", "OPEN").eq(
            "registration_open", True
        ).execute()
        if not res.data:
            await interaction.followup.send("⚠️  الفعالية غير موجودة أو التسجيل مغلق مسبقًا.", ephemeral=True)
            return
        ev = res.data[0]
        await _refresh_event_announcement(self.bot, client, event_id)
        await interaction.followup.send(
            f"🔒  تم إيقاف التسجيلات الجديدة في **{ev['title']}** فقط. الفعالية ما زالت مفتوحة.",
            ephemeral=True,
        )

    @app_commands.command(name="list", description="عرض الفعاليات المفتوحة")
    async def list_events(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.bot.db.get_client()
        rows = (
            await client.table("events").select("id,title,starts_at,capacity,registration_open")
            .eq("guild_id", interaction.guild.id).eq("status", "OPEN")
            .order("starts_at").limit(10).execute()
        ).data or []
        if not rows:
            return await interaction.followup.send("لا توجد فعاليات مفتوحة حالياً.", ephemeral=True)

        count_responses = await asyncio.gather(*(
            client.table("event_participants")
            .select("id", count="exact")
            .eq("event_id", int(event["id"]))
            .eq("status", "REGISTERED")
            .execute()
            for event in rows
        ))
        registered_counts = {
            int(event["id"]): int(response.count or 0)
            for event, response in zip(rows, count_responses)
        }

        embed = discord.Embed(title="🎪  الفعاليات القادمة", color=discord.Color.blurple())
        for ev in rows:
            count = registered_counts[int(ev["id"])]
            ts = int(datetime.fromisoformat(ev["starts_at"].replace("Z", "+00:00")).timestamp())
            bar = _seats_bar(count, ev["capacity"])
            embed.add_field(
                name=f"#{ev['id']}  ·  {ev['title']}",
                value=(
                    f"<t:{ts}:F>\n`{count}/{ev['capacity']}` {bar}"
                    + ("\n🔒 التسجيل مغلق" if not ev.get("registration_open", True) else "")
                ),
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

    @raffle.command(name="create", description="إنشاء سحب وتحديد قناة نشر القائمة والنتيجة")
    @app_commands.describe(
        title="اسم المسابقة",
        channel="القناة التي ستُنشر فيها القائمة وإثبات السحب والفائزون",
        winner_count="عدد الفائزين من 1 إلى 20",
    )
    async def create_raffle(
        self,
        interaction: discord.Interaction,
        title: str,
        channel: discord.TextChannel,
        winner_count: app_commands.Range[int, 1, 20] = 1,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        if not title.strip() or len(title.strip()) > 100:
            return await interaction.response.send_message("يجب أن يكون اسم المسابقة بين 1 و100 حرف.", ephemeral=True)
        if channel.guild.id != interaction.guild.id:
            return await interaction.response.send_message("اختر قناة من هذا السيرفر.", ephemeral=True)
        bot_member = interaction.guild.me
        if bot_member is None:
            return await interaction.response.send_message("تعذر التحقق من صلاحيات البوت.", ephemeral=True)
        permissions = channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links or not permissions.attach_files:
            return await interaction.response.send_message(
                "يحتاج البوت صلاحيات إرسال الرسائل والـEmbeds وإرفاق الملفات في القناة المحددة.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raffle = await self.bot.db.create_event_raffle(
                guild_id=interaction.guild.id,
                created_by=interaction.user.id,
                title=title.strip(),
                channel_id=channel.id,
                winner_count=int(winner_count),
            )
        except Exception:
            logger.exception("Could not create EventHub raffle for guild %s", interaction.guild.id)
            return await interaction.followup.send(
                "تعذر إنشاء السحب. تأكد من تطبيق `eventhub_raffle_migration.sql` ثم حاول مجددًا.",
                ephemeral=True,
            )

        setup_embed = discord.Embed(
            title=f"🎟️ مسابقة سحب جديدة #{raffle['id']} · {raffle['title']}",
            description=(
                f"عدد الفائزين: `{raffle['winner_count']}`\n"
                "سيضيف المشرفون الأعضاء الذين تحققوا من استيفائهم للشروط. "
                "بعدها تُنشر القائمة وسجل تغييراتها قبل اختيار الفائزين.\n"
                "لن يمكن تعديل القائمة بعد تثبيتها، وسيستخدم السحب جولة عشوائية عامة قابلة للتحقق."
            ),
            color=discord.Color.blurple(),
        )
        setup_embed.set_footer(text="القائمة النهائية وإثبات النتيجة سينشران في هذه القناة")
        try:
            await channel.send(
                embed=setup_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Raffle %s was created but its announcement could not be posted", raffle["id"])
            return await interaction.followup.send(
                f"تم إنشاء السحب `#{raffle['id']}` لكن تعذر نشر إعلانه في {channel.mention}. "
                "لم يُنشأ سحب آخر؛ تحقق من صلاحيات القناة.",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"✅ تم إنشاء السحب **#{raffle['id']} · {raffle['title']}**.\n"
            f"قناة إعلان القائمة والنتيجة: {channel.mention}\n"
            f"عدد الفائزين: `{raffle['winner_count']}`\n"
            "اختر المؤهلين من القائمة أدناه أو أضفهم عبر `/event raffle add`، "
            "ثم استخدم `/event raffle lock` لتثبيت القائمة.",
            view=RaffleEntrantAddView(self, int(raffle["id"])),
            ephemeral=True,
        )

    @raffle.command(name="add", description="إضافة عضو مؤهل إلى قائمة السحب")
    @app_commands.describe(raffle_id="رقم السحب", member="العضو المؤهل")
    async def add_raffle_entrant(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        member: discord.Member,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("لا يمكن إضافة حساب بوت إلى السحب.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot.db.manage_event_raffle_entry(
                raffle_id=raffle_id,
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                user_id=member.id,
                user_name=member.display_name,
                action="ADD",
            )
        except Exception:
            logger.exception("Could not add entrant %s to raffle %s", member.id, raffle_id)
            return await interaction.followup.send(
                "لم تتم الإضافة. تأكد من رقم السحب وأنه ما زال مفتوحًا، "
                "وتحقق من أن العضو غير مضاف مسبقًا.",
                ephemeral=True,
            )
        if result.get("already_present"):
            return await interaction.followup.send(
                f"العضو {member.mention} موجود مسبقًا في السحب `#{raffle_id}`.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await interaction.followup.send(
            f"✅ تمت إضافة {member.mention} إلى السحب `#{raffle_id}`. سجل البوت المشرف الذي أضافه.",
            ephemeral=True,
        )

    @raffle.command(name="remove", description="إزالة عضو من سحب مفتوح مع تسجيل السبب")
    @app_commands.describe(raffle_id="رقم السحب", member="العضو المراد إزالته", reason="سبب الإزالة")
    async def remove_raffle_entrant(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        member: discord.Member,
        reason: str = "لم يعد مستوفيًا للشروط",
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.db.manage_event_raffle_entry(
                raffle_id=raffle_id,
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                user_id=member.id,
                user_name=member.display_name,
                action="REMOVE",
                reason=reason,
            )
        except Exception:
            logger.exception("Could not remove entrant %s from raffle %s", member.id, raffle_id)
            return await interaction.followup.send(
                "لم تتم الإزالة. تأكد أن السحب مفتوح وأن العضو موجود في قائمته.",
                ephemeral=True,
            )
        await interaction.followup.send(
            f"✅ تمت إزالة {member.mention} من السحب `#{raffle_id}` وتسجيل السبب في سجل التدقيق.",
            ephemeral=True,
        )

    @raffle.command(name="list", description="عرض قائمة المؤهلين وسجل تغييرات الإدارة")
    @app_commands.describe(raffle_id="رقم السحب")
    async def list_raffle_entrants(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raffle = await self.bot.db.get_event_raffle(raffle_id, interaction.guild.id)
            if not raffle:
                return await interaction.followup.send("لم يتم العثور على هذا السحب في السيرفر.", ephemeral=True)
            entrants, entrant_count = await self.bot.db.get_event_raffle_entries(
                raffle_id,
                interaction.guild.id,
            )
            audit = await self.bot.db.get_event_raffle_audit(raffle_id, interaction.guild.id)
        except Exception:
            logger.exception("Could not list raffle %s", raffle_id)
            return await interaction.followup.send("تعذر تحميل بيانات السحب وسجل التدقيق.", ephemeral=True)

        embed = discord.Embed(
            title=f"📋 المؤهلون للسحب #{raffle_id} · {raffle['title']}",
            description=f"الحالة: `{raffle['status']}` · عدد المؤهلين الحالي: `{entrant_count}`",
            color=discord.Color.blurple(),
        )
        if entrants:
            lines = [
                f"`{index}.` <@{row['user_id']}> · أضافه <@{row['added_by']}>"
                for index, row in enumerate(entrants[:25], start=1)
            ]
            embed.add_field(name="قائمة المشاركين", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="قائمة المشاركين", value="لا يوجد مشاركون حاليًا.", inline=False)

        audit_lines = [
            f"`{row['action']}` بواسطة <@{row['actor_id']}>"
            + (f" · العضو <@{row['target_user_id']}>" if row.get("target_user_id") else "")
            for row in audit[-10:]
        ]
        if audit_lines:
            embed.add_field(name="آخر إجراءات الإدارة", value="\n".join(audit_lines), inline=False)
        if entrant_count > 25:
            embed.set_footer(text="يعرض الأمر أول 25 مشاركًا فقط؛ القائمة الكاملة ستُنشر عند تثبيت السحب.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @raffle.command(name="lock", description="تثبيت قائمة المؤهلين ونشر بصمتها قبل السحب")
    @app_commands.describe(raffle_id="رقم السحب")
    async def lock_raffle(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            existing_raffle = await self.bot.db.get_event_raffle(
                raffle_id,
                interaction.guild.id,
            )
            if not existing_raffle:
                return await interaction.followup.send("لم يتم العثور على هذا السحب.", ephemeral=True)
            if not existing_raffle.get("channel_id"):
                return await interaction.followup.send(
                    "اختر قناة السحب أولًا من زر **إعداد السحب** في لوحة الفعاليات.",
                    ephemeral=True,
                )
            raffle = await self.bot.db.lock_event_raffle(
                raffle_id,
                interaction.guild.id,
                interaction.user.id,
            )
            if raffle.get("status") == "DRAWN":
                return await interaction.followup.send("هذا السحب انتهى بالفعل.", ephemeral=True)

            entrants = raffle.get("entrants_snapshot") or []
            if not raffle.get("entrants_hash"):
                info = await _fetch_drand_info()
                entrants_hash = _raffle_entrants_hash(entrants)
                target_round = max(
                    _raffle_target_round(raffle["locked_at"], info),
                    _raffle_target_round(datetime.now(timezone.utc).isoformat(), info),
                )
                raffle = await self.bot.db.prepare_event_raffle_beacon(
                    raffle_id=raffle_id,
                    guild_id=interaction.guild.id,
                    actor_id=interaction.user.id,
                    entrants_hash=entrants_hash,
                    chain_hash=str(info["hash"]).lower(),
                    target_round=target_round,
                )

            channel = self.bot.get_channel(int(raffle["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError("The configured raffle channel is unavailable.")

            if raffle.get("lock_message_id"):
                if not raffle.get("spin_message_id"):
                    await self._publish_raffle_wheel(raffle, channel)
                return await interaction.followup.send(
                    f"🔒 القائمة مثبتة ومنشورة في {channel.mention}، والعجلة جاهزة للتدوير. "
                    f"موعد العشوائية العام: drand round `{raffle['beacon_target_round']}`.",
                    ephemeral=True,
                )

            manifest_buffer = io.StringIO(newline="")
            writer = csv.writer(manifest_buffer)
            writer.writerow(("discord_user_id", "username"))
            for entrant in entrants:
                writer.writerow((entrant["user_id"], _safe_raffle_csv_text(entrant["user_name"])))

            audit_rows = await self.bot.db.get_event_raffle_audit(
                raffle_id,
                interaction.guild.id,
            )
            audit_buffer = io.StringIO(newline="")
            audit_writer = csv.writer(audit_buffer)
            audit_writer.writerow(("timestamp_utc", "action", "actor_id", "target_user_id", "details"))
            for row in audit_rows:
                details = row.get("details") or {}
                audit_writer.writerow((
                    row.get("created_at", ""),
                    row.get("action", ""),
                    row.get("actor_id", ""),
                    row.get("target_user_id") or "",
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                ))
            manifest_files = [
                discord.File(
                    io.BytesIO(manifest_buffer.getvalue().encode("utf-8-sig")),
                    filename=f"raffle-{raffle_id}-entrants.csv",
                ),
                discord.File(
                    io.BytesIO(audit_buffer.getvalue().encode("utf-8-sig")),
                    filename=f"raffle-{raffle_id}-audit.csv",
                ),
            ]
            lock_embed = discord.Embed(
                title=f"🔒 تثبيت قائمة السحب #{raffle_id} · {raffle['title']}",
                description=(
                    f"تم إغلاق القائمة نهائيًا. لا يمكن إضافة أعضاء أو حذفهم بعد هذه اللحظة.\n"
                    f"**المؤهلون:** `{len(entrants)}` · **الفائزون:** `{raffle['winner_count']}`\n"
                    f"**بصمة القائمة SHA-256:**\n`{raffle['entrants_hash']}`\n"
                    f"**مصدر العشوائية:** drand · chain `{raffle['beacon_chain_hash']}`\n"
                    f"**الجولة المحددة مسبقًا:** `{raffle['beacon_target_round']}`\n\n"
                    "قائمة المشاركين وسجل الإضافة/الإزالة مع أسماء المشرفين مرفقان. "
                    "بعد ظهور الجولة المحددة فقط يمكن تنفيذ السحب."
                ),
                color=discord.Color.gold(),
            )
            lock_embed.set_footer(text="القائمة مجمدة قبل صدور العشوائية العامة")
            message = await channel.send(
                embed=lock_embed,
                files=manifest_files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.bot.db.set_event_raffle_lock_message(
                raffle_id,
                interaction.guild.id,
                message.id,
            )
            raffle["lock_message_id"] = message.id
            await self._publish_raffle_wheel(raffle, channel)
            await interaction.followup.send(
                f"✅ تم تثبيت القائمة ونشرها في {channel.mention}، والعجلة جاهزة.\n"
                f"يستطيع المالك أو المدير أو منشئ السحب الإداري الضغط على زر التدوير بعد صدور drand round "
                f"`{raffle['beacon_target_round']}`.",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Could not lock or publish entrant manifest for raffle %s", raffle_id)
            await interaction.followup.send(
                "تعذر تثبيت القائمة أو نشر إثباتها. لم يُنفذ أي سحب؛ "
                "يمكن إعادة `/event raffle lock` وسيحافظ على القائمة المقفلة نفسها.",
                ephemeral=True,
            )

    async def _publish_raffle_wheel(
        self,
        raffle: dict[str, Any],
        channel: discord.TextChannel,
    ) -> discord.Message:
        if raffle.get("spin_message_id"):
            message = await channel.fetch_message(int(raffle["spin_message_id"]))
            return message
        entrants = raffle.get("entrants_snapshot") or []
        if not entrants:
            raise RuntimeError("The locked raffle has no entrants to display.")
        image_buffer = await asyncio.to_thread(_raffle_wheel_image, entrants)
        embed = discord.Embed(
            title=f"🎡 عجلة السحب #{raffle['id']} · {raffle['title']}",
            description=(
                f"المؤهلون: `{len(entrants)}` · الفائزون: `{raffle['winner_count']}`\n"
                f"قائمة المشاركين مثبتة ببصمة `{raffle['entrants_hash']}`.\n"
                "اضغط زر التدوير بعد وقت العشوائية المحدد. العجلة عرض مرئي فقط؛ "
                "النتيجة تُحسم من جولة drand المحفوظة ولا يمكن إعادة السحب."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_image(url="attachment://raffle-wheel.png")
        embed.set_footer(text=f"drand round {raffle['beacon_target_round']} · لا يبدأ الدوران تلقائيًا")
        view = RaffleSpinView(self, int(raffle["id"]))
        message = await channel.send(
            embed=embed,
            file=discord.File(image_buffer, filename="raffle-wheel.png"),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        stored = await self.bot.db.set_event_raffle_spin_message(
            int(raffle["id"]),
            int(raffle["guild_id"]),
            message.id,
        )
        if not stored:
            current = await self.bot.db.get_event_raffle(
                int(raffle["id"]),
                int(raffle["guild_id"]),
            )
            current_message_id = int((current or {}).get("spin_message_id") or 0)
            if not current_message_id:
                raise RuntimeError("The wheel message was sent but could not be registered.")
            if current_message_id != message.id:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.exception("Could not remove duplicate wheel message for raffle %s", raffle["id"])
                message = await channel.fetch_message(current_message_id)
        self.bot.add_view(view, message_id=message.id)
        raffle["spin_message_id"] = message.id
        return message

    async def _edit_raffle_wheel(
        self,
        message: discord.Message,
        raffle: dict[str, Any],
        entrants: list[dict[str, Any]],
        pointer_index: int,
        *,
        description: str,
        disabled: bool,
    ) -> None:
        image_buffer = await asyncio.to_thread(_raffle_wheel_image, entrants, pointer_index)
        embed = discord.Embed(
            title=f"🎡 عجلة السحب #{raffle['id']} · {raffle['title']}",
            description=description,
            color=discord.Color.green() if disabled else discord.Color.blurple(),
        )
        embed.set_image(url="attachment://raffle-wheel.png")
        embed.set_footer(text=f"قائمة مثبتة · drand round {raffle['beacon_target_round']}")
        await message.edit(
            embed=embed,
            attachments=[discord.File(image_buffer, filename="raffle-wheel.png")],
            view=RaffleSpinView(self, int(raffle["id"]), disabled=disabled),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _deliver_raffle_claim_code(
        self,
        raffle: dict[str, Any],
        user_id: int,
        token: str,
    ) -> bool:
        user = self.bot.get_user(user_id)
        if user is None:
            user = await self.bot.fetch_user(user_id)
        embed = discord.Embed(
            title=f"🎉 مبروك! فزت في {raffle['title']}",
            description=(
                f"رقم السحب: `#{raffle['id']}`\n"
                f"رمز استلام جائزتك: `{token}`\n\n"
                "احتفظ بالرمز ولا تشاركه علنًا. أرسله إلى إدارة السيرفر عند استلام الجائزة. "
                "ستتحقق الإدارة منه مرة واحدة فقط."
            ),
            color=discord.Color.gold(),
        )
        try:
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            logger.info("Could not DM raffle claim code to winner %s for raffle %s", user_id, raffle["id"])
            return False

    async def _publish_raffle_result(
        self,
        interaction: discord.Interaction,
        raffle: dict[str, Any],
    ) -> None:
        if raffle.get("result_message_id"):
            await interaction.followup.send(
                f"تم نشر نتيجة السحب مسبقًا: <#{raffle['channel_id']}>.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(int(raffle["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("The configured raffle channel is unavailable.")

        winners = [int(user_id) for user_id in (raffle.get("winner_ids") or [])]
        winner_text = "\n".join(
            f"🏆 **الفائز {index}:** <@{user_id}>"
            for index, user_id in enumerate(winners, start=1)
        )
        entrants = raffle.get("entrants_snapshot") or []
        proof_text = (
            "Algorithm version: AdSentry-EventHub-Raffle-v1\n"
            "Entrant canonicalization: UTF-8 compact JSON array sorted by user_id; object keys sorted.\n"
            "Selection: SHA-256(domain || randomness_bytes || "
            "raffle_id|entrants_hash|draw_index|nonce), unsigned big-endian, "
            "with rejection sampling; remove each selected winner before the next pick.\n"
            "domain: AdSentry-EventHub-Raffle-v1\n"
            f"Raffle ID: {raffle['id']}\n"
            f"Entrants SHA-256: {raffle['entrants_hash']}\n"
            f"drand chain hash: {raffle['beacon_chain_hash']}\n"
            f"drand round: {raffle['beacon_target_round']}\n"
            f"drand randomness: {raffle['beacon_randomness']}\n"
            f"drand signature: {raffle['beacon_signature']}\n"
            f"Winners: {', '.join(map(str, winners))}\n"
        )
        embed = discord.Embed(
            title=f"🎉 نتيجة السحب #{raffle['id']} · {raffle['title']}",
            description=(
                f"{winner_text}\n\n"
                f"**عدد المؤهلين:** `{len(entrants)}`\n"
                f"**بصمة قائمة المؤهلين:** `{raffle['entrants_hash']}`\n"
                f"**drand round:** [`{raffle['beacon_target_round']}`]"
                f"(https://api.drand.sh/public/{raffle['beacon_target_round']})\n"
                f"**العشوائية العامة:** `{raffle['beacon_randomness']}`\n"
                f"**توقيع الجولة:** `{raffle['beacon_signature']}`\n\n"
                "تحقق من النتيجة عبر `/event raffle verify`؛ العجلة عرض مرئي فقط ولا تختار الفائز. "
                "أُرسل رمز استلام الجائزة للفائز عبر الخاص."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="السحب حُسم من قائمة مجمدة وجولة drand عامة محددة قبل صدورها")
        proof_file = discord.File(
            io.BytesIO(proof_text.encode("utf-8")),
            filename=f"raffle-{raffle['id']}-proof.txt",
        )
        result_message = await channel.send(
            content=" ".join(f"<@{user_id}>" for user_id in winners),
            embed=embed,
            file=proof_file,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await self.bot.db.set_event_raffle_result_message(
            int(raffle["id"]),
            int(raffle["guild_id"]),
            result_message.id,
        )
        await interaction.followup.send(
            f"✅ تم نشر النتيجة وإثباتها في {channel.mention}.",
            ephemeral=True,
        )

    async def spin_raffle(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        wheel_message: discord.Message | None,
    ) -> None:
        if not interaction.guild:
            if not interaction.response.is_done():
                await interaction.response.send_message("استخدم هذا الإجراء داخل السيرفر.", ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        draw_lock = self._raffle_draw_locks.setdefault(raffle_id, asyncio.Lock())
        async with draw_lock:
            try:
                raffle = await self.bot.db.get_event_raffle(raffle_id, interaction.guild.id)
                if not raffle:
                    return await interaction.followup.send("لم يتم العثور على هذا السحب.", ephemeral=True)
                if not _can_spin_raffle(interaction, raffle):
                    return await interaction.followup.send(
                        "التدوير متاح لمالك السيرفر أو مديره أو منشئ السحب إذا كان من الطاقم الإداري.",
                        ephemeral=True,
                    )

                if raffle.get("status") == "DRAWN":
                    winners = [int(user_id) for user_id in (raffle.get("winner_ids") or [])]
                    entrants = raffle.get("entrants_snapshot") or []
                    if wheel_message and winners:
                        winner_index = next(
                            index for index, entrant in enumerate(entrants)
                            if int(entrant["user_id"]) == winners[0]
                        )
                        await self._edit_raffle_wheel(
                            wheel_message,
                            raffle,
                            entrants,
                            winner_index,
                            description=f"🏆 الفائز: <@{winners[0]}>\nالنتيجة مثبتة ولا يمكن إعادة السحب.",
                            disabled=True,
                        )
                    return await self._publish_raffle_result(interaction, raffle)
                if raffle.get("status") != "LOCKED":
                    return await interaction.followup.send(
                        "يجب تثبيت قائمة المؤهلين ونشر العجلة أولًا من لوحة الفعاليات.",
                        ephemeral=True,
                    )
                if not raffle.get("lock_message_id") or not raffle.get("spin_message_id"):
                    return await interaction.followup.send(
                        "لم تُنشر القائمة والعجلة بعد. أعد `/event raffle lock` أولًا.",
                        ephemeral=True,
                    )
                if wheel_message and wheel_message.id != int(raffle["spin_message_id"]):
                    return await interaction.followup.send("هذا الزر لا يخص رسالة عجلة السحب المسجلة.", ephemeral=True)

                if wheel_message is None:
                    channel = self.bot.get_channel(int(raffle["channel_id"]))
                    if not isinstance(channel, discord.TextChannel):
                        channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
                    if not isinstance(channel, discord.TextChannel):
                        raise RuntimeError("The configured raffle channel is unavailable.")
                    wheel_message = await channel.fetch_message(int(raffle["spin_message_id"]))

                info = await _fetch_drand_info()
                if str(info["hash"]).lower() != str(raffle["beacon_chain_hash"]).lower():
                    return await interaction.followup.send(
                        "تعذر التحقق من سلسلة drand المثبتة. لم يتغير السحب ويمكن إعادة المحاولة لاحقًا.",
                        ephemeral=True,
                    )
                beacon = await _fetch_drand_beacon(int(raffle["beacon_target_round"]))
                if int(beacon.get("round", 0)) != int(raffle["beacon_target_round"]):
                    return await interaction.followup.send(
                        "جولة العشوائية المحددة لم تصدر بعد. أعد المحاولة لاحقًا؛ لن يتم اختيار جولة بديلة.",
                        ephemeral=True,
                    )
                randomness = str(beacon.get("randomness") or "")
                signature = str(beacon.get("signature") or "")
                if not re.fullmatch(r"[0-9a-fA-F]{64}", randomness) or not re.fullmatch(r"[0-9a-fA-F]{64,512}", signature):
                    return await interaction.followup.send(
                        "رد مصدر العشوائية غير صالح. لم يتم تنفيذ السحب.",
                        ephemeral=True,
                    )
                entrants = raffle.get("entrants_snapshot") or []
                entrants_hash = _raffle_entrants_hash(entrants)
                if entrants_hash != raffle.get("entrants_hash"):
                    return await interaction.followup.send(
                        "بصمة قائمة المشاركين لا تطابق القائمة المقفلة. تم إيقاف السحب لحماية نزاهته.",
                        ephemeral=True,
                    )
                winner_ids = _select_raffle_winners(
                    randomness=randomness,
                    raffle_id=raffle_id,
                    entrants=entrants,
                    winner_count=int(raffle["winner_count"]),
                    entrants_hash=entrants_hash,
                )
                claim_tokens = {
                    user_id: secrets.token_urlsafe(16)
                    for user_id in winner_ids
                }
                claim_hashes = [
                    {
                        "user_id": user_id,
                        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    }
                    for user_id, token in claim_tokens.items()
                ]
                result = await self.bot.db.record_event_raffle_result(
                    raffle_id=raffle_id,
                    guild_id=interaction.guild.id,
                    actor_id=interaction.user.id,
                    winner_ids=winner_ids,
                    entrants_hash=entrants_hash,
                    chain_hash=str(raffle["beacon_chain_hash"]),
                    round_number=int(raffle["beacon_target_round"]),
                    randomness=randomness.lower(),
                    signature=signature.lower(),
                    claim_hashes=claim_hashes,
                )
                raffle = result.get("raffle") or {}
                if not raffle:
                    raise RuntimeError("Database did not return the recorded raffle result.")

                animation_warning = False
                if result.get("changed"):
                    target_index = next(
                        index for index, entrant in enumerate(entrants)
                        if int(entrant["user_id"]) == winner_ids[0]
                    )
                    start_index = int(randomness[:16], 16) % len(entrants)
                    total_steps = len(entrants) * 3 + ((target_index - start_index) % len(entrants))
                    frames = 18
                    try:
                        for frame in range(1, frames + 1):
                            progress = frame / frames
                            eased = 1 - (1 - progress) ** 3
                            offset = total_steps if frame == frames else round(total_steps * eased)
                            pointer_index = (start_index + offset) % len(entrants)
                            await self._edit_raffle_wheel(
                                wheel_message,
                                raffle,
                                entrants,
                                pointer_index,
                                description="🎡 العجلة تدور... النتيجة محسومة مسبقًا من جولة العشوائية العامة.",
                                disabled=True,
                            )
                            if frame < frames:
                                await asyncio.sleep(0.12 + progress * 0.28)
                    except Exception:
                        animation_warning = True
                        logger.exception("Wheel animation failed after raffle %s result was committed", raffle_id)
                    try:
                        await self._edit_raffle_wheel(
                            wheel_message,
                            raffle,
                            entrants,
                            target_index,
                            description=f"🏆 الفائز: <@{winner_ids[0]}>\nالنتيجة مثبتة ولا يمكن إعادة السحب.",
                            disabled=True,
                        )
                    except Exception:
                        animation_warning = True
                        logger.exception("Could not finalize wheel message for raffle %s", raffle_id)
                else:
                    stored_winners = [int(user_id) for user_id in (raffle.get("winner_ids") or [])]
                    target_index = next(
                        index for index, entrant in enumerate(entrants)
                        if int(entrant["user_id"]) == stored_winners[0]
                    )
                    try:
                        await self._edit_raffle_wheel(
                            wheel_message,
                            raffle,
                            entrants,
                            target_index,
                            description=f"🏆 الفائز: <@{stored_winners[0]}>\nالنتيجة مثبتة ولا يمكن إعادة السحب.",
                            disabled=True,
                        )
                    except Exception:
                        animation_warning = True
                        logger.exception("Could not show the stored winner on raffle %s wheel", raffle_id)

                delivery_status = ""
                if result.get("changed"):
                    delivered = 0
                    for user_id, token in claim_tokens.items():
                        try:
                            if await self._deliver_raffle_claim_code(raffle, user_id, token):
                                delivered += 1
                        except discord.HTTPException:
                            logger.exception("Could not deliver claim code for raffle %s winner %s", raffle_id, user_id)
                    failed = len(claim_tokens) - delivered
                    delivery_status = (
                        f"وصلت رموز الاستلام خاصًا إلى `{delivered}` فائز."
                        if not failed else
                        f"تعذر إرسال رمز خاص إلى `{failed}` فائز؛ أعِد إصداره عبر `/event raffle resend-code`."
                    )
                    if animation_warning:
                        delivery_status += " تعذر تحديث بعض إطارات العجلة، لكن النتيجة محفوظة."
                try:
                    await self._publish_raffle_result(interaction, raffle)
                except Exception:
                    logger.exception("Raffle %s result was committed but could not be published", raffle_id)
                    delivery_status += " حُفظت النتيجة لكن تعذر نشر بطاقة الإثبات؛ أعد تنفيذ الإجراء لنشر النتيجة نفسها."
                if delivery_status:
                    await interaction.followup.send(delivery_status, ephemeral=True)
            except aiohttp.ClientResponseError as error:
                if error.status == 404:
                    await interaction.followup.send(
                        "جولة drand المحددة لم تصدر بعد. أعد المحاولة لاحقًا؛ لن يتم اختيار جولة بديلة.",
                        ephemeral=True,
                    )
                else:
                    logger.exception("drand request failed for raffle %s", raffle_id)
                    await interaction.followup.send(
                        "تعذر الوصول إلى مصدر العشوائية. لم يتم تنفيذ السحب؛ أعد المحاولة لاحقًا.",
                        ephemeral=True,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                logger.exception("Could not obtain or validate drand proof for raffle %s", raffle_id)
                await interaction.followup.send(
                    "تعذر التحقق من مصدر العشوائية. لم يتم تنفيذ السحب؛ أعد المحاولة لاحقًا.",
                    ephemeral=True,
                )
            except Exception:
                logger.exception("Could not draw or publish raffle %s", raffle_id)
                await interaction.followup.send(
                    "تعذر إكمال السحب أو نشره. إذا حُفظت النتيجة، فإن إعادة الإجراء تنشر النتيجة نفسها ولا تعيد السحب.",
                    ephemeral=True,
                )

    @raffle.command(name="draw", description="تنفيذ السحب من زر العجلة بعد صدور جولة drand")
    @app_commands.describe(raffle_id="رقم السحب المقفل")
    async def draw_raffle(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
    ) -> None:
        await self.spin_raffle(interaction, raffle_id, None)

    @raffle.command(name="verify", description="التحقق من أن النتيجة تطابق القائمة وجولة drand")
    @app_commands.describe(raffle_id="رقم السحب")
    async def verify_raffle(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
    ) -> None:
        if not interaction.guild:
            return await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raffle = await self.bot.db.get_event_raffle(raffle_id, interaction.guild.id)
            if not raffle:
                return await interaction.followup.send("لم يتم العثور على هذا السحب.", ephemeral=True)
            if raffle.get("status") != "DRAWN":
                return await interaction.followup.send("لم يتم تنفيذ هذا السحب بعد.", ephemeral=True)

            entrants = raffle.get("entrants_snapshot") or []
            hash_matches = _raffle_entrants_hash(entrants) == raffle.get("entrants_hash")
            calculated_winners = _select_raffle_winners(
                randomness=str(raffle["beacon_randomness"]),
                raffle_id=raffle_id,
                entrants=entrants,
                winner_count=int(raffle["winner_count"]),
                entrants_hash=str(raffle["entrants_hash"]),
            )
            stored_winners = [int(user_id) for user_id in (raffle.get("winner_ids") or [])]
            calculation_matches = calculated_winners == stored_winners

            beacon_matches = False
            try:
                info = await _fetch_drand_info()
                beacon = await _fetch_drand_beacon(int(raffle["beacon_target_round"]))
                beacon_matches = (
                    str(info.get("hash", "")).lower() == str(raffle["beacon_chain_hash"]).lower()
                    and int(beacon.get("round", 0)) == int(raffle["beacon_target_round"])
                    and str(beacon.get("randomness", "")).lower() == str(raffle["beacon_randomness"]).lower()
                    and str(beacon.get("signature", "")).lower() == str(raffle["beacon_signature"]).lower()
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                logger.warning("Could not verify raffle %s against live drand endpoint", raffle_id)

            valid = hash_matches and calculation_matches and beacon_matches
            embed = discord.Embed(
                title="🔍 نتيجة التحقق من السحب",
                description=(
                    f"قائمة المشاركين: {'✅ مطابقة' if hash_matches else '❌ غير مطابقة'}\n"
                    f"حساب الفائزين: {'✅ مطابق' if calculation_matches else '❌ غير مطابق'}\n"
                    f"مصدر drand: {'✅ مطابق' if beacon_matches else '⚠️ تعذر التحقق المباشر'}\n\n"
                    f"**الحالة:** {'✅ الإثبات سليم' if valid else '⚠️ الإثبات غير مكتمل أو غير مطابق'}"
                ),
                color=discord.Color.green() if valid else discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (KeyError, TypeError, ValueError):
            logger.exception("Invalid stored proof for raffle %s", raffle_id)
            await interaction.followup.send(
                "تعذر التحقق بسبب نقص أو خلل في بيانات الإثبات.",
                ephemeral=True,
            )

    @raffle.command(name="resend-code", description="إصدار رمز استلام جديد وإرساله للفائز خاصًا")
    @app_commands.describe(raffle_id="رقم السحب", member="الفائز")
    async def resend_raffle_claim_code(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        member: discord.Member,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raffle = await self.bot.db.get_event_raffle(raffle_id, interaction.guild.id)
            if not raffle or raffle.get("status") != "DRAWN":
                return await interaction.followup.send("السحب غير موجود أو لم تُعلن نتيجته.", ephemeral=True)
            token = secrets.token_urlsafe(16)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            await self.bot.db.issue_event_raffle_claim(
                raffle_id,
                interaction.guild.id,
                interaction.user.id,
                member.id,
                token_hash,
            )
            delivered = await self._deliver_raffle_claim_code(raffle, member.id, token)
        except Exception:
            logger.exception("Could not issue raffle claim code for raffle %s and member %s", raffle_id, member.id)
            return await interaction.followup.send(
                "تعذر إصدار الرمز. تأكد أن العضو فائز ولم يستلم جائزته بعد.",
                ephemeral=True,
            )
        if not delivered:
            return await interaction.followup.send(
                "تم إصدار رمز جديد لكن تعذر إرساله خاصًا. اطلب من الفائز فتح الرسائل الخاصة ثم أعد إصدار الرمز.",
                ephemeral=True,
            )
        await interaction.followup.send(
            "تم إرسال رمز استلام جديد للفائز عبر الخاص. أصبح أي رمز سابق غير صالح.",
            ephemeral=True,
        )

    @raffle.command(name="verify-claim", description="التحقق من رمز الفائز وتسجيل استلام الجائزة")
    @app_commands.describe(raffle_id="رقم السحب", member="الفائز", code="رمز الاستلام الذي قدمه الفائز")
    async def verify_raffle_claim(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        member: discord.Member,
        code: str,
    ) -> None:
        if not interaction.guild or not is_admin(interaction.user):
            return await interaction.response.send_message("التحقق من استلام الجوائز متاح لمالك السيرفر والمدير فقط.", ephemeral=True)
        if not code.strip() or len(code) > 100:
            return await interaction.response.send_message("رمز الاستلام غير صالح.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            token_hash = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
            redeemed = await self.bot.db.verify_event_raffle_claim(
                raffle_id,
                interaction.guild.id,
                member.id,
                interaction.user.id,
                token_hash,
            )
        except Exception:
            logger.exception("Could not verify raffle claim for raffle %s and member %s", raffle_id, member.id)
            return await interaction.followup.send("تعذر التحقق من الرمز. تحقق من رقم السحب والترحيل.", ephemeral=True)
        await interaction.followup.send(
            "✅ تم التحقق من الرمز وتسجيل استلام الجائزة."
            if redeemed else
            "❌ الرمز غير صحيح أو سبق استخدامه.",
            ephemeral=True,
        )

    @raffle.command(name="cancel", description="إلغاء سحب مفتوح قبل تثبيت قائمة المؤهلين")
    @app_commands.describe(raffle_id="رقم السحب", reason="سبب الإلغاء")
    async def cancel_raffle(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        reason: str = "إلغاء إداري",
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raffle = await self.bot.db.cancel_event_raffle(
                raffle_id,
                interaction.guild.id,
                interaction.user.id,
                reason,
            )
        except Exception:
            logger.exception("Could not cancel raffle %s", raffle_id)
            return await interaction.followup.send(
                "لا يمكن إلغاء هذا السحب؛ الإلغاء مسموح فقط قبل تثبيت القائمة.",
                ephemeral=True,
            )
        if raffle.get("channel_id"):
            try:
                channel = self.bot.get_channel(int(raffle["channel_id"]))
                if not isinstance(channel, discord.TextChannel):
                    channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    await channel.send(
                        embed=discord.Embed(
                            title=f"🚫 تم إلغاء السحب #{raffle_id} · {raffle['title']}",
                            description=f"بواسطة {interaction.user.mention}\nالسبب: {reason[:300]}",
                            color=discord.Color.red(),
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except discord.HTTPException:
                logger.exception("Raffle %s was cancelled but its announcement could not be posted", raffle_id)
                return await interaction.followup.send(
                    f"تم إلغاء السحب `#{raffle_id}` وتسجيل الإجراء، لكن تعذر نشر إشعار الإلغاء.",
                    ephemeral=True,
                )
        await interaction.followup.send(
            f"تم إلغاء السحب `#{raffle_id}` وتسجيل السبب.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventHub(bot))
