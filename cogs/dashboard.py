"""A single interactive staff dashboard for AdSentry operations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import is_staff
from cogs.recruitment import (
    DEFAULT_RECRUITMENT_ROLES,
    RecruitmentRoleSelectView,
    close_announcement,
)
from cogs.eventhub import RegisterEventView

logger = logging.getLogger("AdSentry.Dashboard")


def _staff(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and is_staff(interaction.user))


class SystemDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="التوظيف والإدارة", emoji="👥", style=discord.ButtonStyle.primary, row=0)
    async def recruitment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_recruitment(interaction, edit=True)

    @discord.ui.button(label="الفعاليات", emoji="📅", style=discord.ButtonStyle.primary, row=0)
    async def events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_events(interaction, edit=True)

    @discord.ui.button(label="إدارة الطاقم", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def staffflow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_staffflow(interaction, edit=True)

    @discord.ui.button(label="إعداد قنوات التقديم", emoji="⚙️", style=discord.ButtonStyle.secondary, row=1)
    async def channel_setup(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_message("اختر حتى ثلاث قنوات خاصة لاستقبال بطاقات المتقدمين. تأكد أن صلاحيات القنوات مخصصة لفريق المراجعة.", view=ReviewChannelSetupView(self.cog), ephemeral=True)

    @discord.ui.button(label="تحديث اللوحة", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class ReviewChannelSetupView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog
        picker = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], min_values=1, max_values=3,
            placeholder="اختر قناة واحدة أو حتى ثلاث قنوات مراجعة",
        )
        picker.callback = self.save_channels  # type: ignore[method-assign]
        self.add_item(picker)

    async def save_channels(self, interaction: discord.Interaction) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        channels = list(interaction.data.get("values", [])) if interaction.data else []
        guild = interaction.guild
        resolved = [guild.get_channel(int(channel_id)) for channel_id in channels]
        if not resolved or any(not isinstance(channel, discord.TextChannel) for channel in resolved):
            await interaction.response.send_message("اختر قنوات نصية من هذا السيرفر.", ephemeral=True); return
        for channel in resolved:
            perms = channel.permissions_for(guild.me)  # type: ignore[union-attr]
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message(f"أحتاج View Channel وSend Messages وEmbed Links في {channel.mention}.", ephemeral=True); return
            if channel.permissions_for(guild.default_role).view_channel:
                await interaction.response.send_message(f"القناة {channel.mention} ظاهرة للجميع. اجعلها خاصة وأزل صلاحية View Channel عن @everyone قبل اعتمادها لمراجعة بيانات المتقدمين.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await self.cog.bot.db.get_client()
            await client.rpc("set_recruitment_review_channels", {
                "p_guild_id": guild.id,
                "p_channel_ids": [channel.id for channel in resolved],
                "p_configured_by": interaction.user.id,
            }).execute()
        except Exception:
            logger.exception("Could not save recruitment review channel settings")
            await interaction.followup.send("تعذر حفظ القنوات. تأكد من تشغيل ترحيل قاعدة البيانات الجديد.", ephemeral=True); return
        await interaction.followup.send("تم حفظ قنوات مراجعة التقديم: " + "، ".join(channel.mention for channel in resolved), ephemeral=True)


class RecruitmentDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="إنشاء تقديم جديد", emoji="➕", style=discord.ButtonStyle.success)
    async def create_opening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await self.cog.bot.db.get_client()
            rows = (await client.table("recruitment_review_channels").select("slot,channel_id").eq("guild_id", interaction.guild.id).order("slot").execute()).data or []
            if not rows:
                await interaction.followup.send("اضبط قناة مراجعة واحدة على الأقل من الصفحة الرئيسية للوحة أولًا.", ephemeral=True); return
            config = await self.cog.bot.db.get_guild_config(interaction.guild.id)
            public_channel = interaction.guild.get_channel(int((config or {}).get("announcement_channel_id") or 0))
            if not isinstance(public_channel, discord.TextChannel):
                await interaction.followup.send("اضبط قناة الإعلان أولًا باستخدام `/ad-setup`.", ephemeral=True); return
            view = OpeningReviewChannelView(self.cog, public_channel, rows)
            await interaction.followup.send("اختر قناة المراجعة التي ستستقبل بطاقات المتقدمين لهذا التقديم:", view=view, ephemeral=True)
        except Exception:
            logger.exception("Could not open recruitment creation flow from dashboard")
            await interaction.followup.send("تعذر فتح نموذج التقديم. تأكد من تشغيل ترحيل لوحة التوظيف وقاعدة البيانات.", ephemeral=True)

    @discord.ui.button(label="تحديث", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_recruitment(interaction, edit=True)

    @discord.ui.button(label="إغلاق تقديم", emoji="🔒", style=discord.ButtonStyle.danger)
    async def close_opening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        rows = (await client.table("recruitments").select("id,title").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("created_at", desc=True).limit(25).execute()).data or []
        if not rows: await interaction.followup.send("لا توجد تقديمات مفتوحة.", ephemeral=True); return
        await interaction.followup.send("اختر التقديم لإغلاقه:", view=RecruitmentCloseSelectView(self.cog, rows), ephemeral=True)

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class OpeningReviewChannelView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", public_channel: discord.TextChannel, rows: list[dict]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.public_channel = public_channel
        self.select = discord.ui.Select(
            placeholder="اختر قناة من القنوات المضبوطة",
            options=[discord.SelectOption(label=f"قناة المراجعة {row['slot']}", value=str(row["channel_id"]), description=f"القناة #{row['channel_id']}") for row in rows[:3]],
        )
        self.select.callback = self.choose  # type: ignore[method-assign]
        self.add_item(self.select)

    async def choose(self, interaction: discord.Interaction) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        review = interaction.guild.get_channel(int(self.select.values[0]))
        if not isinstance(review, discord.TextChannel):
            await interaction.response.send_message("قناة المراجعة لم تعد موجودة. أعد ضبط القنوات.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        options = [discord.SelectOption(label=role, value=role) for role in DEFAULT_RECRUITMENT_ROLES]
        configured = await self.cog.bot.db.get_custom_recruitment_roles(interaction.guild.id)
        existing = {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}
        for row in configured:
            label = row["label"].strip()
            if label and label.casefold() not in existing and len(options) < 24:
                options.append(discord.SelectOption(label=label, value=label)); existing.add(label.casefold())
        options.append(discord.SelectOption(label="أخرى", value="__other__", description="أدخل اسم منصب مخصص"))
        await interaction.followup.send("اختر المنصب المطلوب، ثم أكمل نموذج تفاصيل التقديم:", view=RecruitmentRoleSelectView(options, self.public_channel, review), ephemeral=True)


class EventCreateModal(discord.ui.Modal, title="إنشاء فعالية"):
    title_input = discord.ui.TextInput(label="اسم الفعالية", max_length=100)
    description = discord.ui.TextInput(label="الوصف", style=discord.TextStyle.paragraph, max_length=800)
    starts_at = discord.ui.TextInput(label="الموعد بصيغة ISO مع المنطقة الزمنية", placeholder="2026-11-01T19:00+03:00", max_length=40)
    capacity = discord.ui.TextInput(label="عدد المقاعد", default="25", max_length=3)
    reminder = discord.ui.TextInput(label="التذكير قبل الموعد بالساعات (0 لتعطيله)", default="24", max_length=3)

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        try:
            start = datetime.fromisoformat(self.starts_at.value.replace("Z", "+00:00"))
            if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(timezone.utc)
            capacity, reminder_hours = int(self.capacity.value), int(self.reminder.value)
            if start <= datetime.now(timezone.utc) or not 1 <= capacity <= 500 or not 0 <= reminder_hours <= 168:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("تحقق من الموعد المستقبلي والمقاعد (1–500) والتذكير (0–168).", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = None
        event_id: int | None = None
        try:
            config = await self.cog.bot.db.get_guild_config(interaction.guild.id)
            channel = interaction.guild.get_channel(int((config or {}).get("announcement_channel_id") or 0))
            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send("اضبط قناة الإعلان أولًا عبر `/ad-setup`.", ephemeral=True); return
            client = await self.cog.bot.db.get_client()
            reminder_at = (start - timedelta(hours=reminder_hours)).isoformat() if reminder_hours else None
            result = await client.table("events").insert({"guild_id": interaction.guild.id, "created_by": interaction.user.id, "title": self.title_input.value.strip(), "description": self.description.value.strip(), "channel_id": channel.id, "starts_at": start.isoformat(), "capacity": capacity, "status": "OPEN", "reminder_at": reminder_at}).execute()
            event = result.data[0]
            event_id = int(event["id"])
            embed = discord.Embed(title=self.title_input.value, description=self.description.value, color=discord.Color.blurple())
            embed.add_field(name="الموعد", value=f"<t:{int(start.timestamp())}:F> · <t:{int(start.timestamp())}:R>")
            embed.add_field(name="المقاعد", value=f"0 / {capacity}")
            message = await channel.send(embed=embed, view=RegisterEventView(event_id), allowed_mentions=discord.AllowedMentions.none())
            self.cog.bot.add_view(RegisterEventView(event_id))
            await client.table("events").update({"announcement_message_id": message.id}).eq("id", event["id"]).execute()
            await interaction.followup.send(f"تم إنشاء الفعالية #{event['id']} ونشرها في {channel.mention}.", ephemeral=True)
        except Exception:
            logger.exception("Dashboard event creation failed")
            if event_id and client:
                try: await client.table("events").update({"status": "CANCELLED"}).eq("id", event_id).execute()
                except Exception: logger.exception("Could not roll back incomplete dashboard event %s", event_id)
            await interaction.followup.send("تعذر إنشاء الفعالية. تحقق من ترحيل EventHub وصلاحيات قناة الإعلان.", ephemeral=True)


class EventAttendanceModal(discord.ui.Modal, title="تسجيل الحضور"):
    event_id = discord.ui.TextInput(label="رقم الفعالية", max_length=12)
    member_id = discord.ui.TextInput(label="معرف العضو", max_length=24)
    attended = discord.ui.TextInput(label="الحضور: نعم أو لا", default="نعم", max_length=3)

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(); self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        try:
            event_id, member_id = int(self.event_id.value), int(self.member_id.value)
            value = self.attended.value.strip().casefold()
            if value not in {"نعم", "لا", "yes", "no"}: raise ValueError
            attended = value in {"نعم", "yes"}
        except ValueError:
            await interaction.response.send_message("أدخل رقم فعالية ومعرف عضو صحيحين، واكتب نعم أو لا للحضور.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        result = await client.table("event_participants").update({"attended": attended, "attendance_by": interaction.user.id, "attendance_at": datetime.now(timezone.utc).isoformat()}).eq("event_id", event_id).eq("guild_id", interaction.guild.id).eq("user_id", member_id).execute()
        await interaction.followup.send("تم تحديث الحضور." if result.data else "لم أجد العضو مسجلًا في هذه الفعالية.", ephemeral=True)


class EventCloseSelectView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", events: list[dict]) -> None:
        super().__init__(timeout=120); self.cog = cog
        select = discord.ui.Select(placeholder="اختر فعالية لإغلاق التسجيل", options=[discord.SelectOption(label=f"#{e['id']} · {e['title']}"[:100], value=str(e["id"])) for e in events[:25]])
        select.callback = self.close_event  # type: ignore[method-assign]
        self.add_item(select)

    async def close_event(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        event_id = int(interaction.data["values"][0])
        event = (await client.table("events").select("*").eq("id", event_id).eq("guild_id", interaction.guild.id).limit(1).execute()).data
        if not event or event[0]["status"] != "OPEN":
            await interaction.followup.send("الفعالية غير موجودة أو التسجيل مغلق مسبقًا.", ephemeral=True); return
        await client.table("events").update({"status": "CLOSED", "closed_at": datetime.now(timezone.utc).isoformat()}).eq("id", event_id).eq("guild_id", interaction.guild.id).eq("status", "OPEN").execute()
        channel = self.cog.bot.get_channel(int(event[0]["channel_id"]))
        if isinstance(channel, discord.TextChannel) and event[0].get("announcement_message_id"):
            try:
                message = await channel.fetch_message(int(event[0]["announcement_message_id"]))
                embed = message.embeds[0] if message.embeds else discord.Embed(title=event[0]["title"])
                embed.color = discord.Color.dark_grey(); embed.set_footer(text="التسجيل مغلق")
                await message.edit(embed=embed, view=None)
            except discord.HTTPException: logger.exception("Could not update event announcement after close")
        await interaction.followup.send("أُغلق التسجيل في الفعالية.", ephemeral=True)


class EventDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300); self.cog = cog

    @discord.ui.button(label="إنشاء فعالية", emoji="➕", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_modal(EventCreateModal(self.cog))

    @discord.ui.button(label="قائمة الفعاليات", emoji="📋", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_events(interaction, edit=True)

    @discord.ui.button(label="إغلاق تسجيل فعالية", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        rows = (await client.table("events").select("id,title").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("starts_at").limit(25).execute()).data or []
        if not rows: await interaction.followup.send("لا توجد فعاليات مفتوحة.", ephemeral=True); return
        await interaction.followup.send("اختر الفعالية:", view=EventCloseSelectView(self.cog, rows), ephemeral=True)

    @discord.ui.button(label="تسجيل الحضور", emoji="✅", style=discord.ButtonStyle.secondary)
    async def attendance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_modal(EventAttendanceModal(self.cog))

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class StaffCaseModal(discord.ui.Modal):
    def __init__(self, cog: "DashboardCog", kind: str) -> None:
        super().__init__(title="طلب إجازة" if kind == "إجازة" else "شكوى إدارية")
        self.cog, self.kind = cog, kind
        if kind == "إجازة":
            self.start = discord.ui.TextInput(label="تاريخ البداية YYYY-MM-DD", max_length=10)
            self.end = discord.ui.TextInput(label="تاريخ النهاية YYYY-MM-DD", max_length=10)
            self.details = discord.ui.TextInput(label="سبب الطلب", style=discord.TextStyle.paragraph, max_length=1500)
            self.add_item(self.start); self.add_item(self.end); self.add_item(self.details)
        else:
            self.subject = discord.ui.TextInput(label="موضوع الشكوى", max_length=120)
            self.details = discord.ui.TextInput(label="تفاصيل الشكوى", style=discord.TextStyle.paragraph, max_length=1500)
            self.add_item(self.subject); self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم النموذج داخل السيرفر.", ephemeral=True); return
        start = end = None
        if self.kind == "إجازة":
            try:
                start_date, end_date = datetime.fromisoformat(self.start.value).date(), datetime.fromisoformat(self.end.value).date()
                if end_date < start_date or start_date < datetime.now(timezone.utc).date(): raise ValueError
                start, end = start_date.isoformat(), end_date.isoformat()
                subject = "طلب إجازة"
            except ValueError:
                await interaction.response.send_message("أدخل تاريخين صحيحين بصيغة YYYY-MM-DD.", ephemeral=True); return
        else:
            subject = self.subject.value.strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            config = await self.cog.bot.db.get_guild_config(interaction.guild.id)
            channel = interaction.guild.get_channel(int((config or {}).get("staff_review_channel_id") or 0))
            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send("لم تُضبط قناة مراجعة الطاقم. استخدم `/ad-setup`.", ephemeral=True); return
            client = await self.cog.bot.db.get_client()
            result = await client.table("staff_cases").insert({"guild_id": interaction.guild.id, "user_id": interaction.user.id, "user_name": str(interaction.user), "kind": self.kind, "subject": subject, "details": self.details.value.strip(), "starts_at": start, "ends_at": end, "status": "PENDING"}).execute()
            case = result.data[0]
            embed = discord.Embed(title=f"طلب {self.kind} · #{case['id']}", description=self.details.value[:4000], color=discord.Color.orange())
            embed.add_field(name="الموظف", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
            embed.add_field(name="الموضوع", value=subject, inline=False)
            if start: embed.add_field(name="الفترة", value=f"{start} إلى {end}", inline=False)
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await client.table("staff_cases").update({"review_channel_id": channel.id, "review_message_id": message.id}).eq("id", case["id"]).execute()
            await interaction.followup.send(f"وصل طلبك إلى الإدارة برقم #{case['id']}.", ephemeral=True)
        except Exception:
            logger.exception("Could not submit dashboard staff case")
            await interaction.followup.send("تعذر إرسال الطلب. تحقق من إعدادات قاعدة البيانات والقناة.", ephemeral=True)


class ShiftCreateModal(discord.ui.Modal, title="جدولة مناوبة"):
    member_id = discord.ui.TextInput(label="معرف عضو الطاقم", max_length=24)
    starts_at = discord.ui.TextInput(label="بداية المناوبة ISO مع المنطقة الزمنية", placeholder="2026-11-01T19:00+03:00", max_length=40)
    duration = discord.ui.TextInput(label="المدة بالساعات (1–24)", max_length=2)
    duties = discord.ui.TextInput(label="المهام", style=discord.TextStyle.paragraph, max_length=900)

    def __init__(self, cog: "DashboardCog") -> None: super().__init__(); self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        try:
            member_id, hours = int(self.member_id.value), int(self.duration.value)
            start = datetime.fromisoformat(self.starts_at.value.replace("Z", "+00:00"))
            if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(timezone.utc)
            if not 1 <= hours <= 24 or start <= datetime.now(timezone.utc): raise ValueError
        except ValueError:
            await interaction.response.send_message("تحقق من معرف العضو والموعد المستقبلي والمدة من 1 إلى 24 ساعة.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        result = await client.table("staff_shifts").insert({"guild_id": interaction.guild.id, "user_id": member_id, "created_by": interaction.user.id, "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=hours)).isoformat(), "duties": self.duties.value.strip(), "status": "SCHEDULED"}).execute()
        await client.table("staff_audit").insert({"guild_id": interaction.guild.id, "actor_id": interaction.user.id, "target_user_id": member_id, "action": "SHIFT_SCHEDULED", "details": {"shift_id": result.data[0]["id"], "starts_at": start.isoformat(), "duration_hours": hours, "duties": self.duties.value.strip()}}).execute()
        await interaction.followup.send(f"تمت جدولة المناوبة رقم #{result.data[0]['id']}.", ephemeral=True)


class StaffDecisionModal(discord.ui.Modal):
    note = discord.ui.TextInput(label="ملاحظة القرار", style=discord.TextStyle.paragraph, max_length=900)

    def __init__(self, cog: "DashboardCog", case_id: int, decision: str) -> None:
        super().__init__(title="مراجعة طلب الطاقم")
        self.cog, self.case_id, self.decision = cog, case_id, decision

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        response = await client.rpc("review_staff_case", {"p_case_id": self.case_id, "p_guild_id": interaction.guild.id, "p_reviewer_id": interaction.user.id, "p_decision": self.decision, "p_note": self.note.value.strip()}).execute()
        row = response.data[0] if isinstance(response.data, list) and response.data else response.data
        if not isinstance(row, dict) or not row.get("changed"):
            await interaction.followup.send("الطلب غير موجود أو سبق اتخاذ قرار فيه.", ephemeral=True); return
        case = row["case"]
        channel = self.cog.bot.get_channel(int(case.get("review_channel_id") or 0))
        if isinstance(channel, discord.TextChannel) and case.get("review_message_id"):
            try:
                card = await channel.fetch_message(int(case["review_message_id"]))
                embed = card.embeds[0] if card.embeds else discord.Embed(title=f"طلب الطاقم #{self.case_id}")
                embed.color = discord.Color.green() if self.decision == "APPROVED" else discord.Color.red()
                embed.add_field(name="القرار", value=f"{self.decision} · {interaction.user}\n{self.note.value}", inline=False)
                await card.edit(embed=embed)
            except discord.HTTPException: logger.exception("Could not update staff case card")
        await interaction.followup.send(f"تم حفظ القرار على الطلب #{self.case_id}.", ephemeral=True)


class StaffCaseQueueView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", cases: list[dict]) -> None:
        super().__init__(timeout=180); self.cog, self.case_id = cog, None
        select = discord.ui.Select(placeholder="اختر طلبًا معلقًا", options=[discord.SelectOption(label=f"#{c['id']} · {c['kind']} · {c['user_name']}"[:100], value=str(c["id"])) for c in cases[:25]])
        select.callback = self.select_case  # type: ignore[method-assign]
        self.add_item(select)

    async def select_case(self, interaction: discord.Interaction) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        self.case_id = int(interaction.data["values"][0])
        await interaction.response.edit_message(content=f"تم اختيار الطلب #{self.case_id}. اختر قرار المراجعة:", view=self)

    @discord.ui.button(label="قبول", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.case_id: await interaction.response.send_message("اختر الطلب أولًا.", ephemeral=True); return
        await interaction.response.send_modal(StaffDecisionModal(self.cog, self.case_id, "APPROVED"))

    @discord.ui.button(label="رفض", emoji="❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.case_id: await interaction.response.send_message("اختر الطلب أولًا.", ephemeral=True); return
        await interaction.response.send_modal(StaffDecisionModal(self.cog, self.case_id, "REJECTED"))


class StaffDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300); self.cog = cog

    @discord.ui.button(label="طلب إجازة", emoji="🌴", style=discord.ButtonStyle.primary, row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StaffCaseModal(self.cog, "إجازة"))

    @discord.ui.button(label="شكوى إدارية", emoji="📨", style=discord.ButtonStyle.primary, row=0)
    async def complaint(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StaffCaseModal(self.cog, "شكوى"))

    @discord.ui.button(label="مراجعة الطلبات", emoji="🗂️", style=discord.ButtonStyle.secondary, row=0)
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        cases = (await client.table("staff_cases").select("id,kind,user_name").eq("guild_id", interaction.guild.id).eq("status", "PENDING").order("created_at").limit(25).execute()).data or []
        if not cases: await interaction.followup.send("لا توجد طلبات معلقة.", ephemeral=True); return
        await interaction.followup.send("اختر طلبًا ثم القرار:", view=StaffCaseQueueView(self.cog, cases), ephemeral=True)

    @discord.ui.button(label="جدولة مناوبة", emoji="🕒", style=discord.ButtonStyle.secondary, row=1)
    async def shift_create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_modal(ShiftCreateModal(self.cog))

    @discord.ui.button(label="عرض المناوبات", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def shifts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        shifts = (await client.table("staff_shifts").select("id,user_id,starts_at,ends_at,duties").eq("guild_id", interaction.guild.id).eq("status", "SCHEDULED").order("starts_at").limit(10).execute()).data or []
        embed = discord.Embed(title="المناوبات القادمة", color=discord.Color.blurple())
        for row in shifts:
            start = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00"))
            embed.add_field(name=f"#{row['id']} · <@{row['user_id']}> · <t:{int(start.timestamp())}:f>", value=row["duties"], inline=False)
        if not shifts: embed.description = "لا توجد مناوبات مجدولة."
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="توثيق ترقية", emoji="⬆️", style=discord.ButtonStyle.secondary, row=1)
    async def promote(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_modal(PositionChangeModal(self.cog))

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class RecruitmentCloseSelectView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", openings: list[dict]) -> None:
        super().__init__(timeout=120); self.cog = cog
        select = discord.ui.Select(placeholder="اختر تقديمًا لإغلاقه", options=[discord.SelectOption(label=f"#{row['id']} · {row['title']}"[:100], value=str(row["id"])) for row in openings[:25]])
        select.callback = self.close_opening  # type: ignore[method-assign]
        self.add_item(select)

    async def close_opening(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        opening_id = int(interaction.data["values"][0])
        opening = await self.cog.bot.db.get_recruitment(opening_id)
        if not opening or opening["guild_id"] != interaction.guild.id:
            await interaction.followup.send("التقديم غير موجود.", ephemeral=True); return
        changed = await self.cog.bot.db.close_recruitment(opening_id, interaction.guild.id)
        if changed: await close_announcement(self.cog.bot, opening)
        await interaction.followup.send("تم إغلاق التقديم." if changed else "التقديم مغلق مسبقًا.", ephemeral=True)


class PositionChangeModal(discord.ui.Modal, title="توثيق ترقية أو تغيير منصب"):
    member_id = discord.ui.TextInput(label="معرف العضو", max_length=24)
    previous = discord.ui.TextInput(label="المنصب السابق", max_length=100, required=False)
    new = discord.ui.TextInput(label="المنصب الجديد", max_length=100)
    reason = discord.ui.TextInput(label="السبب", style=discord.TextStyle.paragraph, max_length=900)

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(); self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        try: member_id = int(self.member_id.value)
        except ValueError:
            await interaction.response.send_message("معرف العضو غير صحيح.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        await client.table("staff_audit").insert({"guild_id": interaction.guild.id, "actor_id": interaction.user.id, "target_user_id": member_id, "action": "POSITION_CHANGE", "details": {"previous": self.previous.value or "غير محدد", "new": self.new.value, "reason": self.reason.value}}).execute()
        await interaction.followup.send("تم توثيق تغيير المنصب في سجل الإدارة.", ephemeral=True)


class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def show_home(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("لوحة التحكم متاحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            openings = (await client.table("recruitments").select("id").eq("guild_id", interaction.guild.id).eq("status", "OPEN").execute()).data or []
            events = (await client.table("events").select("id").eq("guild_id", interaction.guild.id).eq("status", "OPEN").execute()).data or []
            cases = (await client.table("staff_cases").select("id").eq("guild_id", interaction.guild.id).eq("status", "PENDING").execute()).data or []
            channels = (await client.table("recruitment_review_channels").select("channel_id").eq("guild_id", interaction.guild.id).order("slot").execute()).data or []
            ads = await self.bot.db.get_ad_stats(interaction.guild.id, days=7)
            channel_text = "، ".join(f"<#{row['channel_id']}>" for row in channels) or "لم يتم ضبطها"
            description = (f"مرحبًا **{interaction.guild.name}**. اختر النظام الذي تريد إدارته من الأزرار.\n\n"
                           f"**التوظيف:** {len(openings)} تقديم مفتوح\n**الفعاليات:** {len(events)} فعالية مفتوحة\n**طلبات الطاقم المعلقة:** {len(cases)}\n"
                           f"**الإعلانات خلال 7 أيام:** {ads['total_count']} (مقبول: {ads['approved_count']} · مرفوض: {ads['rejected_count']})\n"
                           f"**قنوات مراجعة المتقدمين:** {channel_text}\n\n"
                           "اللوحة خاصة بك ولن تظهر للأعضاء الآخرين.")
        except Exception:
            logger.exception("Could not load dashboard summary")
            description = "تعذر تحميل بعض الإحصاءات. تأكد من تشغيل ترحيلات قاعدة البيانات الخاصة بالأنظمة الجديدة."
        embed = discord.Embed(title="AdSentry · لوحة التحكم", description=description, color=discord.Color.blurple())
        view = SystemDashboardView(self)
        await interaction.edit_original_response(embed=embed, view=view)

    async def show_recruitment(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("recruitments").select("id,title,seat_limit,closes_at,review_channel_id").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("created_at", desc=True).limit(8).execute()).data or []
        except Exception:
            logger.exception("Could not load recruitment dashboard page")
            await interaction.edit_original_response(content="تعذر تحميل التقديمات. تحقق من قاعدة البيانات وترحيل التوظيف.", embed=None, view=RecruitmentDashboardView(self))
            return
        if rows:
            lines = [f"• **#{row['id']} {row['title']}** · المقاعد: {row.get('seat_limit') or 'غير محدودة'} · بطاقات المتقدمين: <#{row['review_channel_id']}>" for row in rows]
            body = "\n".join(lines)
        else:
            body = "لا توجد طلبات انضمام مفتوحة حاليًا."
        embed = discord.Embed(title="لوحة التوظيف", description=f"{body}\n\nمن هنا تنشئ تقديمًا، وتحدد قناة مراجعة من القنوات الثلاث المضبوطة.", color=discord.Color.green())
        view = RecruitmentDashboardView(self)
        await interaction.edit_original_response(embed=embed, view=view)

    async def show_events(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("events").select("id,title,starts_at,capacity").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("starts_at").limit(8).execute()).data or []
            details = []
            for event in rows:
                count = (await client.table("event_participants").select("id", count="exact").eq("event_id", event["id"]).execute()).count or 0
                start = datetime.fromisoformat(event["starts_at"].replace("Z", "+00:00"))
                details.append(f"• **#{event['id']} {event['title']}** · <t:{int(start.timestamp())}:f> · {count}/{event['capacity']} مقعد")
            description = "\n".join(details) if details else "لا توجد فعاليات مفتوحة حاليًا."
            description += "\n\nاختر إجراءً من الأزرار لإدارة الفعاليات والتسجيل والحضور."
        except Exception:
            logger.exception("Could not load EventHub dashboard page")
            description = "تعذر تحميل الفعاليات. تأكد من تشغيل ترحيل EventHub."
        embed = discord.Embed(title="EventHub · إدارة الفعاليات", description=description, color=discord.Color.blurple())
        await interaction.edit_original_response(embed=embed, view=EventDashboardView(self))

    async def show_staffflow(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            cases = (await client.table("staff_cases").select("id").eq("guild_id", interaction.guild.id).eq("status", "PENDING").execute()).data or []
            shifts = (await client.table("staff_shifts").select("id").eq("guild_id", interaction.guild.id).eq("status", "SCHEDULED").execute()).data or []
            description = f"طلبات بانتظار المراجعة: **{len(cases)}**\nمناوبات مجدولة: **{len(shifts)}**\n\nاستخدم النماذج والأزرار أدناه لتقديم الطلبات ومراجعتها وجدولة المناوبات."
        except Exception:
            logger.exception("Could not load StaffFlow dashboard page")
            description = "تعذر تحميل بيانات الطاقم. تأكد من تشغيل ترحيل StaffFlow."
        embed = discord.Embed(title="StaffFlow · إدارة الطاقم", description=description, color=discord.Color.blurple())
        await interaction.edit_original_response(embed=embed, view=StaffDashboardView(self))

    @app_commands.command(name="dashboard", description="فتح لوحة تحكم أنظمة AdSentry")
    async def dashboard(self, interaction: discord.Interaction) -> None:
        await self.show_home(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DashboardCog(bot))
