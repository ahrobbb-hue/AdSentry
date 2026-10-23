"""EventHub: server events, capacity-safe registration and attendance."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.moderation import is_staff

logger = logging.getLogger("AdSentry.EventHub")


class RegisterEventView(discord.ui.View):
    def __init__(self, event_id: int) -> None:
        super().__init__(timeout=None)
        button = discord.ui.Button(label="سجّل في الفعالية", style=discord.ButtonStyle.primary, custom_id=f"eventhub:register:{event_id}")
        button.callback = self._register  # type: ignore[method-assign]
        self.add_item(button)
        self.event_id = event_id

    async def _register(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم هذا الزر داخل السيرفر.", ephemeral=True); return
        try:
            client = await interaction.client.db.get_client()  # type: ignore[attr-defined]
            response = await client.rpc("register_event_participant", {"p_event_id": self.event_id, "p_guild_id": interaction.guild.id, "p_user_id": interaction.user.id, "p_user_name": str(interaction.user)}).execute()
            data = response.data[0] if isinstance(response.data, list) else response.data
            messages = {"registered": "تم تسجيلك في الفعالية.", "full": "اكتملت المقاعد.", "duplicate": "أنت مسجل بالفعل.", "closed": "التسجيل مغلق أو انتهت الفعالية.", "not_found": "الفعالية غير موجودة."}
            await interaction.response.send_message(messages.get(data.get("result"), "تعذر إتمام التسجيل."), ephemeral=True)
        except Exception:
            logger.exception("Event button registration failed")
            if not interaction.response.is_done(): await interaction.response.send_message("حدث خطأ أثناء التسجيل.", ephemeral=True)


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
            await client.table("events").update({"status": "CLOSED", "closed_at": now}).eq("status", "OPEN").lte("starts_at", now).execute()
            rows = (await client.table("events").select("*").eq("status", "OPEN")
                    .not_.is_("reminder_at", "null").lte("reminder_at", now)
                    .is_("reminded_at", "null").execute()).data or []
            for event in rows:
                # Claim the reminder atomically before sending to prevent duplicates after reconnects.
                claimed = await client.table("events").update({"reminded_at": now}).eq("id", event["id"]).is_("reminded_at", "null").execute()
                if not claimed.data:
                    continue
                channel = self.bot.get_channel(int(event["channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    await channel.send(f"⏰ تذكير: فعالية **{event['title']}** تبدأ قريبًا! <t:{int(datetime.fromisoformat(event['starts_at'].replace('Z', '+00:00')).timestamp())}:R>", allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Event reminder sweep failed")

    @reminder_sweeper.before_loop
    async def before_reminder_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="create", description="إنشاء فعالية جديدة")
    @app_commands.describe(title="اسم الفعالية", starts_at="الموعد بصيغة ISO مثل 2026-11-01T19:00", capacity="عدد المقاعد", reminder_hours="التذكير قبل الموعد بساعات، 0 لتعطيله")
    async def create(self, interaction: discord.Interaction, title: str, description: str, channel: discord.TextChannel, starts_at: str, capacity: app_commands.Range[int, 1, 500] = 25, reminder_hours: app_commands.Range[int, 0, 168] = 24) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        try:
            start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(timezone.utc)
            if start <= datetime.now(timezone.utc): raise ValueError
        except ValueError:
            await interaction.response.send_message("أدخل موعدًا مستقبليًا بصيغة ISO، مثال: `2026-11-01T19:00+03:00`.", ephemeral=True); return
        if channel.guild.id != interaction.guild.id or not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message("اختر قناة من هذا السيرفر يستطيع البوت الإرسال فيها.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        client = await self.bot.db.get_client()
        reminder_at = (start - timedelta(hours=reminder_hours)).isoformat() if reminder_hours else None
        result = await client.table("events").insert({"guild_id": interaction.guild.id, "created_by": interaction.user.id, "title": title[:100], "description": description[:1800], "channel_id": channel.id, "starts_at": start.isoformat(), "capacity": capacity, "status": "OPEN", "reminder_at": reminder_at}).execute()
        event = result.data[0]
        embed = discord.Embed(title=title[:256], description=description[:4000], color=discord.Color.blurple())
        embed.add_field(name="الموعد", value=f"<t:{int(start.timestamp())}:F>\n<t:{int(start.timestamp())}:R>")
        embed.add_field(name="المقاعد", value=f"0 / {capacity}")
        try:
            message = await channel.send(embed=embed, view=RegisterEventView(int(event["id"])), allowed_mentions=discord.AllowedMentions.none())
            self.bot.add_view(RegisterEventView(int(event["id"])))
            await client.table("events").update({"announcement_message_id": message.id}).eq("id", event["id"]).execute()
        except Exception:
            await client.table("events").update({"status": "CANCELLED"}).eq("id", event["id"]).execute()
            raise
        await interaction.followup.send(f"تم إنشاء الفعالية #{event['id']} في {channel.mention}.", ephemeral=True)

    @app_commands.command(name="register", description="التسجيل في فعالية")
    async def register(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        try:
            client = await self.bot.db.get_client()
            response = await client.rpc("register_event_participant", {"p_event_id": event_id, "p_guild_id": interaction.guild.id, "p_user_id": interaction.user.id, "p_user_name": str(interaction.user)}).execute()
            data = response.data[0] if isinstance(response.data, list) else response.data
            messages = {"registered": "تم تسجيلك في الفعالية.", "full": "اكتملت المقاعد.", "duplicate": "أنت مسجل بالفعل.", "closed": "التسجيل مغلق أو انتهت الفعالية.", "not_found": "لم أجد هذه الفعالية في السيرفر."}
            await interaction.response.send_message(messages.get(data.get("result"), "تعذر إتمام التسجيل."), ephemeral=True)
        except Exception:
            logger.exception("Event registration failed")
            if not interaction.response.is_done(): await interaction.response.send_message("حدث خطأ أثناء التسجيل.", ephemeral=True)

    @app_commands.command(name="attendance", description="تسجيل حضور مشارك")
    async def attendance(self, interaction: discord.Interaction, event_id: int, member: discord.Member, attended: bool = True) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        client = await self.bot.db.get_client()
        res = await client.table("event_participants").update({"attended": attended, "attendance_by": interaction.user.id, "attendance_at": datetime.now(timezone.utc).isoformat()}).eq("event_id", event_id).eq("guild_id", interaction.guild.id).eq("user_id", member.id).execute()
        await interaction.response.send_message("تم تحديث الحضور." if res.data else "هذا العضو غير مسجل في الفعالية.", ephemeral=True)

    @app_commands.command(name="close", description="إغلاق التسجيل في فعالية")
    async def close(self, interaction: discord.Interaction, event_id: int) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        client = await self.bot.db.get_client()
        res = await client.table("events").update({"status": "CLOSED", "closed_at": datetime.now(timezone.utc).isoformat()}).eq("id", event_id).eq("guild_id", interaction.guild.id).eq("status", "OPEN").execute()
        await interaction.response.send_message("تم إغلاق التسجيل." if res.data else "الفعالية غير موجودة أو مغلقة.", ephemeral=True)

    @app_commands.command(name="list", description="عرض الفعاليات القادمة")
    async def list_events(self, interaction: discord.Interaction) -> None:
        if not interaction.guild: await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        client = await self.bot.db.get_client()
        rows = (await client.table("events").select("id,title,starts_at,capacity,status").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("starts_at").limit(10).execute()).data or []
        if not rows: await interaction.response.send_message("لا توجد فعاليات مفتوحة.", ephemeral=True); return
        embed = discord.Embed(title="الفعاليات القادمة", color=discord.Color.blurple())
        for event in rows:
            count = (await client.table("event_participants").select("id", count="exact").eq("event_id", event["id"]).execute()).count or 0
            ts = int(datetime.fromisoformat(event["starts_at"].replace("Z", "+00:00")).timestamp())
            embed.add_field(name=f"#{event['id']} · {event['title']}", value=f"<t:{ts}:F> · المقاعد {count}/{event['capacity']}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventHub(bot))
