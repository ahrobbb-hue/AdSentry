"""EventHub: server events, capacity-safe registration and attendance."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.moderation import is_staff

logger = logging.getLogger("AdSentry.EventHub")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

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
) -> discord.Embed:
    """Return a polished event announcement embed."""
    color_map = {
        "OPEN": discord.Color.from_rgb(88, 101, 242),   # blurple
        "CLOSED": discord.Color.from_rgb(128, 128, 128),
        "CANCELLED": discord.Color.red(),
    }
    color = color_map.get(status, discord.Color.blurple())

    spots_left = capacity - registered
    seats_bar = _seats_bar(registered, capacity)

    embed = discord.Embed(
        title=f"🎪  {title}",
        description=description,
        color=color,
    )
    embed.add_field(
        name="📅  الموعد",
        value=f"<t:{int(start.timestamp())}:F>\n<t:{int(start.timestamp())}:R>",
        inline=True,
    )
    embed.add_field(
        name="🎟️  المقاعد",
        value=f"{seats_bar}\n`{registered} / {capacity}` · متبقي **{spots_left}**",
        inline=True,
    )
    if organizer:
        embed.add_field(name="👤  المنظّم", value=organizer.mention, inline=True)
    if event_id:
        embed.set_footer(text=f"رقم الفعالية: #{event_id}  •  AdSentry EventHub")
    if status == "CLOSED":
        embed.set_footer(text="🔒  التسجيل مغلق  •  AdSentry EventHub")
    if image_url:
        embed.set_image(url=image_url)
    return embed


def _seats_bar(registered: int, capacity: int, width: int = 10) -> str:
    """Return a unicode progress bar for seats."""
    if capacity <= 0:
        return "░" * width
    filled = round(registered / capacity * width)
    return "█" * filled + "░" * (width - filled)


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
            messages = {
                "registered": "✅  تم تسجيلك في الفعالية بنجاح!",
                "full": "❌  اكتملت المقاعد.",
                "duplicate": "ℹ️  أنت مسجل بالفعل في هذه الفعالية.",
                "closed": "🔒  التسجيل مغلق أو انتهت الفعالية.",
                "not_found": "⚠️  الفعالية غير موجودة.",
            }
            await interaction.response.send_message(
                messages.get(result, "تعذر إتمام التسجيل."), ephemeral=True
            )
            # Send admin notification on successful registration
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
                            # Update the public embed seat count
                            await _refresh_event_announcement(
                                interaction.client,  # type: ignore[arg-type]
                                client, self.event_id
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
        event_row = (await client.table("events").select(  # type: ignore[attr-defined]
            "title,description,starts_at,capacity,channel_id,announcement_message_id,status,created_by"
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
        start = datetime.fromisoformat(ev["starts_at"].replace("Z", "+00:00"))
        organizer = bot.get_user(int(ev["created_by"])) if ev.get("created_by") else None
        embed = build_event_embed(
            ev["title"], ev["description"], start,
            ev["capacity"], registered,
            event_id=event_id,
            organizer=organizer,
            status=ev.get("status", "OPEN"),
        )
        message = await channel.fetch_message(int(ev["announcement_message_id"]))
        view = RegisterEventView(event_id) if ev.get("status") == "OPEN" else None
        await message.edit(embed=embed, view=view)
    except Exception:
        logger.warning("Could not refresh event announcement embed for event %s", event_id)


class EventHub(commands.GroupCog, group_name="event"):
    """Manage events and participant lists."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminder_sweeper.start()
        try:
            client = await self.bot.db.get_client()
            events = (await client.table("events").select("id").eq("status", "OPEN").execute()).data or []
            for event in events:
                self.bot.add_view(RegisterEventView(int(event["id"])))
        except Exception:
            logger.exception("Could not restore persistent event registration buttons")

    def cog_unload(self) -> None:
        self.reminder_sweeper.cancel()

    @tasks.loop(minutes=1)
    async def reminder_sweeper(self) -> None:
        try:
            client = await self.bot.db.get_client()
            now = datetime.now(timezone.utc).isoformat()
            # Auto-close events that have passed their start time
            expired = (await client.table("events").update(
                {"status": "CLOSED", "closed_at": now}
            ).eq("status", "OPEN").lte("starts_at", now).execute()).data or []
            # Update embeds and notify admin for auto-closed events
            for ev in expired:
                try:
                    count = (await client.table("event_participants").select(
                        "id", count="exact"
                    ).eq("event_id", ev["id"]).execute()).count or 0
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

    @app_commands.command(name="create", description="إنشاء فعالية جديدة")
    @app_commands.describe(
        title="اسم الفعالية",
        description="وصف الفعالية",
        channel="قناة نشر الفعالية العامة",
        admin_channel="القناة الإدارية الخاصة لتلقي إشعارات التسجيل (اختياري)",
        starts_at="الموعد بصيغة ISO مثل 2026-11-01T19:00+03:00",
        capacity="عدد المقاعد",
        reminder_hours="التذكير قبل الموعد بساعات، 0 لتعطيله",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        channel: discord.TextChannel,
        starts_at: str,
        admin_channel: discord.TextChannel | None = None,
        capacity: app_commands.Range[int, 1, 500] = 25,
        reminder_hours: app_commands.Range[int, 0, 168] = 24,
    ) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        try:
            start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(timezone.utc)
            if start <= datetime.now(timezone.utc): raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "أدخل موعدًا مستقبليًا بصيغة ISO، مثال: `2026-11-01T19:00+03:00`.", ephemeral=True
            ); return
        if channel.guild.id != interaction.guild.id or not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(
                "اختر قناة من هذا السيرفر يستطيع البوت الإرسال فيها.", ephemeral=True
            ); return
        if admin_channel and (admin_channel.guild.id != interaction.guild.id or
                              not admin_channel.permissions_for(interaction.guild.me).send_messages):
            await interaction.response.send_message(
                "القناة الإدارية يجب أن تكون من هذا السيرفر ويستطيع البوت الإرسال فيها.", ephemeral=True
            ); return
        await interaction.response.defer(ephemeral=True)
        client = await self.bot.db.get_client()
        reminder_at = (start - timedelta(hours=reminder_hours)).isoformat() if reminder_hours else None
        result = await client.table("events").insert({
            "guild_id": interaction.guild.id,
            "created_by": interaction.user.id,
            "title": title[:100],
            "description": description[:1800],
            "channel_id": channel.id,
            "admin_channel_id": admin_channel.id if admin_channel else None,
            "starts_at": start.isoformat(),
            "capacity": capacity,
            "status": "OPEN",
            "reminder_at": reminder_at,
        }).execute()
        event = result.data[0]
        event_id = int(event["id"])
        embed = build_event_embed(
            title, description, start, capacity,
            event_id=event_id,
            organizer=interaction.user,
        )
        try:
            message = await channel.send(
                embed=embed,
                view=RegisterEventView(event_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.bot.add_view(RegisterEventView(event_id))
            await client.table("events").update({"announcement_message_id": message.id}).eq("id", event["id"]).execute()
        except Exception:
            await client.table("events").update({"status": "CANCELLED"}).eq("id", event["id"]).execute()
            raise
        admin_note = f" · إشعارات الإدارة في {admin_channel.mention}" if admin_channel else ""
        await interaction.followup.send(
            f"✅ تم إنشاء الفعالية **#{event_id}** ونشرها في {channel.mention}{admin_note}.",
            ephemeral=True,
        )

    @app_commands.command(name="register", description="التسجيل في فعالية")
    async def register(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        try:
            client = await self.bot.db.get_client()
            response = await client.rpc("register_event_participant", {
                "p_event_id": event_id, "p_guild_id": interaction.guild.id,
                "p_user_id": interaction.user.id, "p_user_name": str(interaction.user),
            }).execute()
            data = response.data[0] if isinstance(response.data, list) else response.data
            result = data.get("result")
            messages = {
                "registered": "✅  تم تسجيلك في الفعالية بنجاح!",
                "full": "❌  اكتملت المقاعد.",
                "duplicate": "ℹ️  أنت مسجل بالفعل.",
                "closed": "🔒  التسجيل مغلق أو انتهت الفعالية.",
                "not_found": "⚠️  لم أجد هذه الفعالية في السيرفر.",
            }
            await interaction.response.send_message(messages.get(result, "تعذر إتمام التسجيل."), ephemeral=True)
            if result == "registered":
                try:
                    ev_row = (await client.table("events").select(
                        "title,capacity,admin_channel_id"
                    ).eq("id", event_id).limit(1).execute()).data
                    if ev_row and ev_row[0].get("admin_channel_id"):
                        ev = ev_row[0]
                        reg_count = (await client.table("event_participants").select(
                            "id", count="exact"
                        ).eq("event_id", event_id).execute()).count or 0
                        await send_admin_event_notification(
                            self.bot, int(ev["admin_channel_id"]),
                            event_id=event_id, event_title=ev["title"],
                            kind="new_registration", user=interaction.user,
                            registered=reg_count, capacity=ev["capacity"],
                            guild=interaction.guild,
                        )
                        await _refresh_event_announcement(self.bot, client, event_id)
                except Exception:
                    logger.exception("Admin notification after slash register failed")
        except Exception:
            logger.exception("Event registration failed")
            if not interaction.response.is_done():
                await interaction.response.send_message("حدث خطأ أثناء التسجيل.", ephemeral=True)

    @app_commands.command(name="attendance", description="تسجيل حضور مشارك")
    async def attendance(self, interaction: discord.Interaction, event_id: int, member: discord.Member, attended: bool = True) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        client = await self.bot.db.get_client()
        res = await client.table("event_participants").update({
            "attended": attended,
            "attendance_by": interaction.user.id,
            "attendance_at": datetime.now(timezone.utc).isoformat(),
        }).eq("event_id", event_id).eq("guild_id", interaction.guild.id).eq("user_id", member.id).execute()
        await interaction.response.send_message(
            "✅  تم تحديث الحضور." if res.data else "⚠️  هذا العضو غير مسجل في الفعالية.", ephemeral=True
        )

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
        await _refresh_event_announcement(self.bot, client, event_id)
        if ev.get("admin_channel_id"):
            await send_admin_event_notification(
                self.bot, int(ev["admin_channel_id"]),
                event_id=event_id, event_title=ev["title"],
                kind="event_closed", registered=reg_count, capacity=ev["capacity"],
            )
        await interaction.followup.send("🔒  تم إغلاق التسجيل في الفعالية.", ephemeral=True)

    @app_commands.command(name="list", description="عرض الفعاليات القادمة")
    async def list_events(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        client = await self.bot.db.get_client()
        rows = (await client.table("events").select(
            "id,title,starts_at,capacity,status,channel_id,admin_channel_id"
        ).eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("starts_at").limit(10).execute()).data or []
        if not rows:
            await interaction.response.send_message("لا توجد فعاليات مفتوحة.", ephemeral=True); return
        embed = discord.Embed(title="🎪  الفعاليات القادمة", color=discord.Color.blurple())
        for event in rows:
            count = (await client.table("event_participants").select(
                "id", count="exact"
            ).eq("event_id", event["id"]).execute()).count or 0
            ts = int(datetime.fromisoformat(event["starts_at"].replace("Z", "+00:00")).timestamp())
            admin_note = f" · 🔔 <#{event['admin_channel_id']}>" if event.get("admin_channel_id") else ""
            embed.add_field(
                name=f"#{event['id']} · {event['title']}",
                value=f"<t:{ts}:F> · `{count}/{event['capacity']}` مقعد{admin_note}",
                inline=False,
            )
        embed.set_footer(text="AdSentry EventHub")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventHub(bot))
