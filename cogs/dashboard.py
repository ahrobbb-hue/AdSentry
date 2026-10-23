"""A single interactive staff dashboard for AdSentry operations."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import is_staff
from cogs.recruitment import (
    DEFAULT_RECRUITMENT_ROLES,
    RecruitmentRoleSelectView,
)

logger = logging.getLogger("AdSentry.Dashboard")


def _staff(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and is_staff(interaction.user))


async def _edit(interaction: discord.Interaction, title: str, description: str, view: discord.ui.View | None = None) -> None:
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


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
        await _edit(interaction, "EventHub · إدارة الفعاليات", "من هنا تدير الفعاليات والتسجيل والمقاعد والحضور.\n\n**الأوامر المتاحة:** `/event create` لإنشاء فعالية، `/event list` للقائمة، `/event attendance` للحضور، `/event close` للإغلاق.\n\nسيتم دمج إنشاء الفعالية في نموذج اللوحة في مرحلة لاحقة؛ جميع هذه الأوامر تعمل الآن.", self)

    @discord.ui.button(label="إدارة الطاقم", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def staffflow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await _edit(interaction, "StaffFlow · إدارة الطاقم", "طلبات الإجازات والشكاوى والمناوبات وسجل القرارات.\n\n**الأوامر المتاحة:** `/staffflow leave`، `/staffflow complaint`، `/staffflow review`، `/staffflow shift-create`، `/staffflow shift-list`، `/staffflow promote`.\n\nطلبات الطاقم الحساسة تُرسل إلى قناة المراجعة الخاصة المضبوطة في `/ad-setup`.", self)

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
        client = await self.cog.bot.db.get_client()
        try:
            await client.rpc("set_recruitment_review_channels", {
                "p_guild_id": guild.id,
                "p_channel_ids": [channel.id for channel in resolved],
                "p_configured_by": interaction.user.id,
            }).execute()
        except Exception:
            logger.exception("Could not save recruitment review channel settings")
            await interaction.response.send_message("تعذر حفظ القنوات. تأكد من تشغيل ترحيل قاعدة البيانات الجديد.", ephemeral=True); return
        await interaction.response.send_message("تم حفظ قنوات مراجعة التقديم: " + "، ".join(channel.mention for channel in resolved), ephemeral=True)


class RecruitmentDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="إنشاء تقديم جديد", emoji="➕", style=discord.ButtonStyle.success)
    async def create_opening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        client = await self.cog.bot.db.get_client()
        rows = (await client.table("recruitment_review_channels").select("slot,channel_id").eq("guild_id", interaction.guild.id).order("slot").execute()).data or []
        if not rows:
            await interaction.response.send_message("اضبط قناة مراجعة واحدة على الأقل من الصفحة الرئيسية للوحة أولًا.", ephemeral=True); return
        config = await self.cog.bot.db.get_guild_config(interaction.guild.id)
        public_channel = interaction.guild.get_channel(int((config or {}).get("announcement_channel_id") or 0))
        if not isinstance(public_channel, discord.TextChannel):
            await interaction.response.send_message("اضبط قناة الإعلان أولًا باستخدام `/ad-setup`.", ephemeral=True); return
        view = OpeningReviewChannelView(self.cog, public_channel, rows)
        await interaction.response.send_message("اختر قناة المراجعة التي ستستقبل بطاقات المتقدمين لهذا التقديم:", view=view, ephemeral=True)

    @discord.ui.button(label="تحديث", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_recruitment(interaction, edit=True)

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
        options = [discord.SelectOption(label=role, value=role) for role in DEFAULT_RECRUITMENT_ROLES]
        configured = await self.cog.bot.db.get_custom_recruitment_roles(interaction.guild.id)
        existing = {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}
        for row in configured:
            label = row["label"].strip()
            if label and label.casefold() not in existing and len(options) < 24:
                options.append(discord.SelectOption(label=label, value=label)); existing.add(label.casefold())
        options.append(discord.SelectOption(label="أخرى", value="__other__", description="أدخل اسم منصب مخصص"))
        await interaction.response.send_message("اختر المنصب المطلوب، ثم أكمل نموذج تفاصيل التقديم:", view=RecruitmentRoleSelectView(options, self.public_channel, review), ephemeral=True)


class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def show_home(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("لوحة التحكم متاحة للإدارة فقط.", ephemeral=True); return
        client = await self.bot.db.get_client()
        try:
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
        if edit and interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        elif edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def show_recruitment(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        client = await self.bot.db.get_client()
        rows = (await client.table("recruitments").select("id,title,seat_limit,closes_at,review_channel_id").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("created_at", desc=True).limit(8).execute()).data or []
        if rows:
            lines = [f"• **#{row['id']} {row['title']}** · المقاعد: {row.get('seat_limit') or 'غير محدودة'} · بطاقات المتقدمين: <#{row['review_channel_id']}>" for row in rows]
            body = "\n".join(lines)
        else:
            body = "لا توجد طلبات انضمام مفتوحة حاليًا."
        embed = discord.Embed(title="لوحة التوظيف", description=f"{body}\n\nمن هنا تنشئ تقديمًا، وتحدد قناة مراجعة من القنوات الثلاث المضبوطة.", color=discord.Color.green())
        view = RecruitmentDashboardView(self)
        if edit and interaction.response.is_done(): await interaction.edit_original_response(embed=embed, view=view)
        elif edit: await interaction.response.edit_message(embed=embed, view=view)
        else: await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="dashboard", description="فتح لوحة تحكم أنظمة AdSentry")
    async def dashboard(self, interaction: discord.Interaction) -> None:
        await self.show_home(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DashboardCog(bot))
