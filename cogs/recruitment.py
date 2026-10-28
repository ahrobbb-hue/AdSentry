"""Staff-managed recruitment openings and private application reviews."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.moderation import is_admin

logger = logging.getLogger("AdSentry.Recruitment")

DEFAULT_RECRUITMENT_ROLES = (
    "🎫 Ticket Support",
    "🛡️ Moderator",
    "💼 Management",
    "👑 Administrator",
    "🎪 Events Team",
    "🎨 Design Team",
)


def get_applicant_role_info(role_name: str) -> tuple[str, int]:
    """
    Returns (applicant_role_name, recommended_slot) for a given recruitment role name.
    All role names are strictly in English with expressive emojis.
    Slot 1 = join-requests-1 (Low / Entry ranks: Ticket Support, Events Team, Design Team, Helper)
    Slot 2 = join-requests-2 (Medium ranks: Moderator, Management, Supervisor)
    Slot 3 = join-requests-3 (High ranks: Administrator, Head Mod, Executive, Owner)
    """
    import re
    clean_name = role_name.strip()
    low = clean_name.lower()

    # High ranks -> Slot 3 (join-requests-3)
    if any(k in low for k in ["admin", "administrator", "owner", "executive", "co-owner", "رئيس", "أدمن", "ادمن", "مدير عام"]):
        return "👑 Applicant | Administrator", 3

    # Medium ranks -> Slot 2 (join-requests-2)
    if any(k in low for k in ["management", "manager", "supervisor", "مانجر", "مشرف", "إدارة", "ادارة"]):
        return "💼 Applicant | Management", 2
    if any(k in low for k in ["moderator", "mod", "مراقب"]):
        return "🛡️ Applicant | Moderator", 2

    # Low / Entry ranks -> Slot 1 (join-requests-1)
    if any(k in low for k in ["ticket", "support", "helper", "تكت", "دعم", "مساعد"]):
        return "🎫 Applicant | Ticket Support", 1
    if any(k in low for k in ["event", "events", "فعاليات"]):
        return "🎪 Applicant | Events Team", 1
    if any(k in low for k in ["design", "designer", "media", "تصميم", "مصمم"]):
        return "🎨 Applicant | Design Team", 1

    # Default for custom roles: format as "📋 Applicant | <English Name>"
    cleaned = re.sub(r'^[^\w\s]+', '', clean_name).strip() or clean_name
    return f"📋 Applicant | {cleaned}", 1


async def resolve_review_channel(guild: discord.Guild, bot: commands.Bot, slot: int) -> discord.TextChannel | None:
    """Find the review channel for the given slot (1, 2, or 3)."""
    # 1. Check database recruitment_review_channels
    try:
        client = await bot.db.get_client() if hasattr(bot.db, "get_client") else getattr(bot.db, "client", None)
        if client:
            res = (
                await client.table("recruitment_review_channels")
                .select("channel_id")
                .eq("guild_id", guild.id)
                .eq("slot", slot)
                .maybe_single()
                .execute()
            )
            if res and res.data and res.data.get("channel_id"):
                ch = guild.get_channel(int(res.data["channel_id"]))
                if isinstance(ch, discord.TextChannel):
                    return ch
    except Exception:
        pass

    # 2. Match exact slot channel name e.g. join-requests-1 / join-requests-2 / join-requests-3
    needle = f"join-requests-{slot}"
    for ch in guild.text_channels:
        if needle in ch.name.lower():
            return ch

    # 3. Fallback to any join-requests channel
    for ch in guild.text_channels:
        if "join-requests" in ch.name.lower() or "طلبات-الانضمام" in ch.name:
            return ch

    return None


async def resolve_staff_review_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Find the ads-and-staff channel where review cards (Accept/Reject/Interview) are posted."""
    for ch in guild.text_channels:
        name = ch.name.lower().replace(" ", "-").replace("_", "-")
        if "ads-and-staff" in name or "ads_and_staff" in name:
            return ch
    return None


def _can_manage(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and is_admin(interaction.user))


def _deadline_passed(opening: dict[str, Any]) -> bool:
    raw = opening.get("closes_at")
    if not raw:
        return False
    deadline = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return deadline <= datetime.now(timezone.utc)


class RecruitmentDetailsModal(discord.ui.Modal):
    def __init__(self, title: str | None, public_channel: discord.TextChannel, review_channel: discord.TextChannel | None, bot: commands.Bot, grant_role: discord.Role | None = None) -> None:
        super().__init__(title="Create recruitment opening", timeout=600)
        self.role_title = title
        self.public_channel = public_channel
        self.review_channel = review_channel
        self.bot = bot
        self.grant_role = grant_role
        if title is None:
            self.custom_title = discord.ui.TextInput(label="Custom role name", placeholder="e.g. Community Manager", max_length=80, required=True)
            self.add_item(self.custom_title)
        self.description = discord.ui.TextInput(label="Description and requirements", style=discord.TextStyle.paragraph, placeholder="Describe the role, responsibilities, and requirements.", max_length=1200, required=True)
        self.seat_limit = discord.ui.TextInput(label="Number of people to accept (1–100)", default="1", max_length=3, required=True)
        self.close_after = discord.ui.TextInput(label="Close after hours (optional)", placeholder="Leave blank for manual close; maximum 720", max_length=3, required=False)
        self.questions = discord.ui.TextInput(label="Extra questions (optional, one per line)", style=discord.TextStyle.paragraph, placeholder="Up to 2 extra questions", max_length=92, required=False)
        self.add_item(self.description)
        self.add_item(self.seat_limit)
        self.add_item(self.close_after)
        self.add_item(self.questions)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return
        title = self.role_title or self.custom_title.value.strip()
        description = self.description.value.strip()
        try:
            seat_limit = int(self.seat_limit.value.strip())
            duration_value = (self.close_after.value or "").strip()
            duration_hours = int(duration_value) if duration_value else None
        except ValueError:
            await interaction.response.send_message("Seats must be a number, and the close duration must be a whole number of hours.", ephemeral=True)
            return
        if not 1 <= seat_limit <= 100 or (duration_hours is not None and not 1 <= duration_hours <= 720):
            await interaction.response.send_message("Choose 1–100 seats and an optional closing period of 1–720 hours.", ephemeral=True)
            return
        questions = [line.strip() for line in (self.questions.value or "").splitlines() if line.strip()]
        if len(questions) > 2 or any(len(question) > 45 for question in questions):
            await interaction.response.send_message("Add at most two extra questions, each no longer than 45 characters.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        target_review = self.review_channel
        if not target_review and interaction.guild:
            _, slot = get_applicant_role_info(title)
            target_review = await resolve_review_channel(interaction.guild, self.bot, slot)
            if not target_review:
                target_review = self.public_channel

        try:
            await publish_opening(self.bot, interaction, title, description, self.public_channel, target_review,
                                  seat_limit=seat_limit, duration_hours=duration_hours, questions=questions, grant_role=self.grant_role)
            await interaction.followup.send("✅ تم فتح التقديم بنجاح!", ephemeral=True)
        except ValueError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        except PermissionError as e:
            await interaction.followup.send(f"⛔ مشكلة صلاحيات: {e}", ephemeral=True)
        except Exception as e:
            logger.exception("Could not create recruitment through the role picker")
            await interaction.followup.send(
                f"❌ تعذّر فتح التقديم.\nالخطأ: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )


class RecruitmentRoleSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], public_channel: discord.TextChannel, review_channel: discord.TextChannel | None, grant_role: discord.Role | None = None) -> None:
        super().__init__(placeholder="Choose the role you are recruiting for", min_values=1, max_values=1, options=options, row=0)
        self.public_channel = public_channel
        self.review_channel = review_channel
        self.grant_role = grant_role

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return
        bot: commands.Bot = interaction.client  # type: ignore[assignment]
        value = self.values[0]
        chosen_title = None if value == "__other__" else value

        target_review = self.review_channel
        if not target_review and interaction.guild:
            _, slot = get_applicant_role_info(chosen_title or "")
            target_review = await resolve_review_channel(interaction.guild, bot, slot)
            if not target_review:
                target_review = self.public_channel

        await interaction.response.send_modal(RecruitmentDetailsModal(
            title=chosen_title,
            public_channel=self.public_channel,
            review_channel=target_review,
            bot=bot,
            grant_role=self.grant_role,
        ))


class RecruitmentRoleSelectView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption], public_channel: discord.TextChannel, review_channel: discord.TextChannel | None = None, grant_role: discord.Role | None = None) -> None:
        super().__init__(timeout=180)
        self.add_item(RecruitmentRoleSelect(options, public_channel, review_channel, grant_role))


class RecruitmentPublishChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, bot: commands.Bot, review_channel: discord.TextChannel | None = None, grant_role: discord.Role | None = None) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="📢 اختر القناة التي تريد نشر الإعلان فيها | Announcement Channel",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.bot = bot
        self.review_channel = review_channel
        self.grant_role = grant_role

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return
        selected_ch = self.values[0]
        if not isinstance(selected_ch, discord.TextChannel):
            selected_ch = interaction.guild.get_channel(selected_ch.id)  # type: ignore[union-attr]
        if not isinstance(selected_ch, discord.TextChannel):
            await interaction.response.send_message("Please select a valid text channel.", ephemeral=True)
            return

        options = [discord.SelectOption(label=role, value=role) for role in DEFAULT_RECRUITMENT_ROLES]
        try:
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            existing = {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}
            for row in configured:
                label = row["label"].strip()
                if label and label.casefold() not in existing and len(options) < 24:
                    options.append(discord.SelectOption(label=label, value=label))
                    existing.add(label.casefold())
        except Exception:
            pass
        options.append(discord.SelectOption(label="📋 Other / Custom Role", value="__other__", description="Enter custom English role"))

        view = RecruitmentRoleSelectView(options, selected_ch, self.review_channel, self.grant_role)
        await interaction.response.edit_message(
            content=f"📢 **تم اختيار قناة النشر:** {selected_ch.mention}\n**الخطوة 2:** اختر الرتبة المطلوبة أدناه (سيتم توجيه الطلبات تلقائياً إلى قناة المراجعة المخصصة لها):",
            view=view,
        )


class RecruitmentPublishChannelPickerView(discord.ui.View):
    def __init__(self, bot: commands.Bot, review_channel: discord.TextChannel | None = None, grant_role: discord.Role | None = None) -> None:
        super().__init__(timeout=300)
        self.add_item(RecruitmentPublishChannelSelect(bot, review_channel, grant_role))


async def publish_opening(
    bot: commands.Bot,
    interaction: discord.Interaction,
    title: str,
    description: str,
    public_channel: discord.TextChannel,
    review_channel: discord.TextChannel,
    *,
    seat_limit: int | None = None,
    duration_hours: int | None = None,
    questions: list[str] | None = None,
    grant_role: discord.Role | None = None,
) -> None:
    """Create the database record and public announcement with rollback on partial failure."""
    assert interaction.guild is not None
    if not title.strip() or len(title) > 80 or not description.strip() or len(description) > 1800:
        raise ValueError("Title or description exceeds the supported length.")
    if public_channel.guild.id != interaction.guild.id or review_channel.guild.id != interaction.guild.id:
        raise ValueError("Both channels must belong to this server.")
    bot_member = interaction.guild.me
    if not bot_member:
        raise RuntimeError("Bot member is unavailable in this server.")
    public_perms = public_channel.permissions_for(bot_member)
    if not public_perms.send_messages or not public_perms.embed_links:
        raise PermissionError("Missing announcement channel permissions.")
    review_perms = review_channel.permissions_for(bot_member)
    # البوت يحتاج فقط أن يرى القناة ويضبط الصلاحيات للمتقدم (ليس إرسال رسائل — بطاقة المراجعة تذهب لـ ads-and-staff)
    if not review_perms.view_channel or not review_perms.manage_channels:
        raise PermissionError(f"البوت يحتاج View Channel و Manage Channel في {review_channel.mention} حتى يفتحها للمتقدمين.")
    if seat_limit is not None and not 1 <= seat_limit <= 100:
        raise ValueError("Seat limit must be between 1 and 100.")
    if duration_hours is not None and not 1 <= duration_hours <= 720:
        raise ValueError("Closing period must be between 1 and 720 hours.")
    if grant_role:
        if grant_role.guild.id != interaction.guild.id or grant_role.is_default() or grant_role.managed:
            raise ValueError("Choose a normal role from this server.")
        if not bot_member.guild_permissions.manage_roles or grant_role >= bot_member.top_role:
            raise PermissionError("The bot needs Manage Roles and a role higher than the role to grant.")
    active = await bot.db.get_open_recruitments(interaction.guild.id)
    if any(row["title"].casefold() == title.casefold() for row in active):
        raise ValueError("An opening with this role name is already active.")
    opening: dict[str, Any] | None = None
    announcement: discord.Message | None = None
    try:
        opening = await bot.db.create_recruitment({
            "guild_id": interaction.guild.id,
            "title": title.strip(),
            "description": description.strip(),
            "created_by": interaction.user.id,
            "public_channel_id": public_channel.id,
            "review_channel_id": review_channel.id,
            "status": "OPEN",
            "seat_limit": seat_limit,
            "closes_at": (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat() if duration_hours else None,
            "questions": questions or [],
            "grant_role_id": grant_role.id if grant_role else None,
        })
        embed = discord.Embed(title=f"Now accepting applications: {title.strip()}", description=description.strip(), color=discord.Color.green())
        details = [f"Seats: {seat_limit}" if seat_limit else None,
                   f"Closes: <t:{int((datetime.now(timezone.utc) + timedelta(hours=duration_hours)).timestamp())}:f>" if duration_hours else "Closes manually"]
        embed.add_field(name="Details", value=" · ".join(value for value in details if value), inline=False)
        if grant_role:
            embed.add_field(name="Role after acceptance", value=grant_role.mention, inline=False)
        embed.set_footer(text=f"Opening #{opening['id']} · Apply using the button below")
        announcement = await public_channel.send(embed=embed, view=ApplyView(opening["id"]), allowed_mentions=discord.AllowedMentions.none())
        await bot.db.set_recruitment_message(opening["id"], announcement.id)
        bot.add_view(ApplyView(opening["id"]))
    except Exception:
        if opening:
            try:
                await bot.db.close_recruitment(opening["id"], interaction.guild.id)
            except Exception:
                logger.exception("Could not roll back incomplete opening %s", opening["id"])
        if announcement:
            try:
                await announcement.edit(view=None)
            except discord.HTTPException:
                logger.warning("Could not disable announcement for incomplete opening")
        raise


class ApplicationModal(discord.ui.Modal):
    def __init__(self, recruitment: dict[str, Any], bot: commands.Bot) -> None:
        super().__init__(title=f"Apply: {recruitment['title']}"[:45], timeout=600)
        self.recruitment = recruitment
        self.bot = bot
        self.experience = discord.ui.TextInput(label="Relevant experience", style=discord.TextStyle.paragraph, max_length=900, required=True)
        self.motivation = discord.ui.TextInput(label="Why do you want this role?", style=discord.TextStyle.paragraph, max_length=900, required=True)
        self.availability = discord.ui.TextInput(label="Availability / timezone", max_length=300, required=True)
        self.add_item(self.experience)
        self.add_item(self.motivation)
        self.add_item(self.availability)
        self.custom_inputs: list[discord.ui.TextInput] = []
        for question in (recruitment.get("questions") or [])[:2]:
            answer = discord.ui.TextInput(label=question, style=discord.TextStyle.paragraph, max_length=700, required=True)
            self.custom_inputs.append(answer)
            self.add_item(answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.guild.id != self.recruitment["guild_id"]:
            await interaction.response.send_message("This application is not available here.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Re-check status at submission time; stale buttons cannot submit to a closed opening.
            current = await self.bot.db.get_recruitment(self.recruitment["id"])
            if not current or current.get("status") != "OPEN" or _deadline_passed(current):
                if current and current.get("status") == "OPEN":
                    await self.bot.db.close_recruitment(current["id"], current["guild_id"])
                    await close_announcement(self.bot, current)
                await interaction.followup.send("This application is closed.", ephemeral=True)
                return
            record = await self.bot.db.create_recruitment_application({
                "recruitment_id": current["id"],
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "user_name": str(interaction.user),
                "experience": self.experience.value.strip(),
                "motivation": self.motivation.value.strip(),
                "availability": self.availability.value.strip(),
                "custom_answers": [{"question": field.label, "answer": field.value.strip()} for field in self.custom_inputs],
                "status": "PENDING",
            })
            if record.get("reason") == "duplicate":
                await interaction.followup.send("You have already applied to this opening.", ephemeral=True)
                return
            if record.get("reason") == "closed":
                latest = await self.bot.db.get_recruitment(current["id"])
                if latest:
                    await close_announcement(self.bot, latest)
                await interaction.followup.send("This application closed before your submission was received.", ephemeral=True)
                return
            # ── الحصول على قناة join-requests الخاصة بالمتقدم ──
            applicant_channel = self.bot.get_channel(current["review_channel_id"])
            if not isinstance(applicant_channel, discord.TextChannel):
                try:
                    applicant_channel = await self.bot.fetch_channel(current["review_channel_id"])
                except Exception:
                    applicant_channel = None

            # ── الحصول على قناة ads-and-staff لنشر بطاقة المراجعة ──
            staff_channel = await resolve_staff_review_channel(interaction.guild)
            if not isinstance(staff_channel, discord.TextChannel):
                # fallback: إذا لم توجد ads-and-staff نرسل لقناة join-requests
                staff_channel = applicant_channel

            if not isinstance(staff_channel, discord.TextChannel):
                raise RuntimeError("Could not find ads-and-staff or any review channel")

            # 1. تحديد رتبة Applicant ومنحها للمتقدم
            applicant_role_name, _ = get_applicant_role_info(current["title"])
            guild = interaction.guild
            applicant_role = discord.utils.get(guild.roles, name=applicant_role_name)
            if not applicant_role and guild.me.guild_permissions.manage_roles:
                try:
                    applicant_role = await guild.create_role(
                        name=applicant_role_name,
                        color=discord.Color.gold(),
                        mentionable=False,
                        reason=f"AdSentry applicant role for {current['title']}",
                    )
                except discord.HTTPException:
                    logger.exception("Could not create applicant role %s", applicant_role_name)

            if applicant_role and applicant_role < guild.me.top_role and isinstance(interaction.user, discord.Member):
                try:
                    await interaction.user.add_roles(applicant_role, reason=f"Applied for {current['title']}")
                except discord.HTTPException:
                    logger.exception("Could not assign applicant role %s", applicant_role_name)

            # 2. فتح قناة join-requests للمتقدم (للنقاش والتكلم مع الإدارة)
            if isinstance(applicant_channel, discord.TextChannel) and isinstance(interaction.user, discord.Member):
                try:
                    await applicant_channel.set_permissions(
                        interaction.user,
                        view_channel=True,
                        read_message_history=True,
                        send_messages=True,
                        reason=f"AdSentry applicant access to #{applicant_channel.name} for #{record['id']}",
                    )
                except discord.HTTPException:
                    logger.exception("Could not set channel overwrites for applicant")

            # 3. إرسال بطاقة المراجعة (مع أزرار Accept/Reject/Interview) إلى ads-and-staff
            embed = discord.Embed(
                title=f"New staff application · #{record['id']}",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            embed.add_field(name="Position applied for", value=f"{current['title']}", inline=False)
            embed.add_field(name="Experience", value=self.experience.value[:1024], inline=False)
            embed.add_field(name="Motivation", value=self.motivation.value[:1024], inline=False)
            embed.add_field(name="Availability", value=self.availability.value[:1024], inline=False)
            for field in self.custom_inputs:
                embed.add_field(name=field.label, value=field.value[:1024], inline=False)
            if isinstance(applicant_channel, discord.TextChannel):
                embed.add_field(name="📋 Application Channel", value=applicant_channel.mention, inline=False)

            await staff_channel.send(
                embed=embed,
                view=ApplicationReviewView(record["id"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )

            # 4. إشعار المتقدم بنجاح تقديم الطلب
            channel_msg = f" وفُتحت لك قناة {applicant_channel.mention} لمتابعة طلبك." if isinstance(applicant_channel, discord.TextChannel) else ""
            role_msg = f" وتم منحك رتبة **{applicant_role_name}**" if applicant_role else ""
            await interaction.followup.send(
                f"✅ تم إرسال طلبك بنجاح إلى الإدارة!{role_msg}{channel_msg}",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("Could not process recruitment application")
            await interaction.followup.send("We couldn't submit your application. Please contact staff and try again later.", ephemeral=True)


class ApplyView(discord.ui.View):
    def __init__(self, recruitment_id: int) -> None:
        super().__init__(timeout=None)
        self.recruitment_id = recruitment_id
        button = discord.ui.Button(label="Apply now", style=discord.ButtonStyle.success, custom_id=f"adsentry:recruit:apply:{recruitment_id}")
        button.callback = self.apply  # type: ignore[method-assign]
        self.add_item(button)

    async def apply(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Applications are only available in the server.", ephemeral=True)
            return
        bot: commands.Bot = interaction.client  # type: ignore[assignment]
        recruitment = await bot.db.get_recruitment(self.recruitment_id)
        if not recruitment or recruitment.get("status") != "OPEN" or recruitment["guild_id"] != interaction.guild.id or _deadline_passed(recruitment):
            if recruitment and recruitment.get("status") == "OPEN" and _deadline_passed(recruitment):
                await bot.db.close_recruitment(recruitment["id"], interaction.guild.id)
                await close_announcement(bot, recruitment)
            await interaction.response.send_message("This application is closed.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(recruitment, bot))


class ApplicationReviewView(discord.ui.View):
    def __init__(self, application_id: int) -> None:
        super().__init__(timeout=None)
        for decision, label, style in (("INTERVIEW", "Invite to interview", discord.ButtonStyle.primary), ("ACCEPTED", "Accept", discord.ButtonStyle.success), ("REJECTED", "Reject", discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"adsentry:recruit:review:{application_id}:{decision}")
            button.callback = self.review  # type: ignore[method-assign]
            self.add_item(button)

    async def review(self, interaction: discord.Interaction) -> None:
        # ── الأزرار تعمل فقط للـ Owner والـ Admin ──
        if not _can_manage(interaction):
            await interaction.response.send_message(
                "⛔ هذه الأزرار متاحة للأونر والأدمن فقط.",
                ephemeral=True,
            )
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        parts = custom_id.split(":")
        if len(parts) != 5 or not parts[3].isdigit() or parts[4] not in {"INTERVIEW", "ACCEPTED", "REJECTED"}:
            await interaction.response.send_message("Invalid review action.", ephemeral=True)
            return
        application_id, decision = int(parts[3]), parts[4]
        bot: commands.Bot = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        try:
            application = await bot.db.get_recruitment_application(application_id, interaction.guild.id)  # type: ignore[union-attr]
            if not application:
                await interaction.followup.send("Application not found in this server.", ephemeral=True)
                return
            opening = await bot.db.get_recruitment(application["recruitment_id"])
            result = await bot.db.review_recruitment_application(application_id, interaction.user.id, decision, interaction.guild.id)  # type: ignore[union-attr]
            if not result.get("changed"):
                reason = result.get("reason")
                message = "No more seats are available; this opening has reached its capacity." if reason == "capacity" else "This application was already reviewed or is no longer available."
                await interaction.followup.send(message, ephemeral=True)
                return

            # ── تحديث بطاقة المراجعة في ads-and-staff ──
            if interaction.message:
                embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title=f"Application #{application_id}")
                embed.color = (
                    discord.Color.blurple() if decision == "INTERVIEW"
                    else discord.Color.green() if decision == "ACCEPTED"
                    else discord.Color.red()
                )
                decision_label = {"INTERVIEW": "📞 مقابلة صوتية", "ACCEPTED": "✅ تم القبول", "REJECTED": "❌ تم الرفض"}.get(decision, decision.title())
                embed.add_field(
                    name="القرار | Decision",
                    value=f"{decision_label} بواسطة {interaction.user.mention}",
                    inline=False,
                )
                try:
                    await interaction.message.edit(
                        embed=embed,
                        view=None if decision != "INTERVIEW" else ApplicationReviewView(application_id),
                    )
                except discord.HTTPException:
                    logger.exception("Decision saved but review card could not be updated for application %s", application_id)

            # ── جلب عضو المتقدم ──
            member = interaction.guild.get_member(application["user_id"]) if interaction.guild else None  # type: ignore[union-attr]
            if not member and interaction.guild:
                try:
                    member = await interaction.guild.fetch_member(application["user_id"])  # type: ignore[union-attr]
                except (discord.HTTPException, discord.NotFound):
                    member = None

            # ── عند القبول أو الرفض: سحب رتبة Applicant المؤقتة وإغلاق قناة الطلب ──
            role_result = ""
            app_role = None
            if decision in {"ACCEPTED", "REJECTED"}:
                if member and interaction.guild:
                    applicant_role_name, _ = get_applicant_role_info(opening.get("title", "")) if opening else ("", 1)
                    if applicant_role_name:
                        app_role = discord.utils.get(interaction.guild.roles, name=applicant_role_name)
                        if app_role and app_role in member.roles:
                            bot_member = interaction.guild.me
                            if (
                                bot_member
                                and app_role < bot_member.top_role
                                and bot_member.guild_permissions.manage_roles
                            ):
                                try:
                                    await member.remove_roles(app_role, reason=f"Recruitment #{application_id} {decision}")
                                    logger.info("Removed applicant role %s from %s", applicant_role_name, member.id)
                                    role_result += f"\n✅ تم سحب رتبة التقديم المؤقتة **{applicant_role_name}**."
                                except discord.HTTPException:
                                    logger.exception("Could not remove applicant role %s", applicant_role_name)
                                    role_result += f"\n⚠️ تعذّر سحب رتبة التقديم المؤقتة **{applicant_role_name}**؛ يرجى سحبها يدوياً."
                            else:
                                role_result += f"\n⚠️ لا يملك البوت صلاحية سحب رتبة التقديم المؤقتة **{applicant_role_name}**؛ يرجى سحبها يدوياً."
                        else:
                            role_result += f"\nℹ️ رتبة التقديم المؤقتة **{applicant_role_name}** غير موجودة لدى المتقدم."
                    else:
                        role_result += "\n⚠️ تعذّر تحديد رتبة التقديم المؤقتة لسحبها."

                    # ── إزالة صلاحية المتقدم من قناة join-requests ──
                    if opening and opening.get("review_channel_id"):
                        applicant_ch = interaction.guild.get_channel(int(opening["review_channel_id"]))
                        if isinstance(applicant_ch, discord.TextChannel):
                            try:
                                await applicant_ch.set_permissions(
                                    member,
                                    overwrite=None,
                                    reason=f"Recruitment #{application_id} {decision} — removing applicant access",
                                )
                                role_result += "\n✅ تم إلغاء صلاحية المتقدم من قناة الطلب."
                            except discord.HTTPException:
                                logger.exception("Could not remove join-requests overwrite for applicant %s", member.id)
                                role_result += "\n⚠️ تعذّر إلغاء صلاحية المتقدم من قناة الطلب؛ يرجى إزالتها يدوياً."
                else:
                    role_result += "\n⚠️ تعذّر العثور على المتقدم لسحب رتبته المؤقتة أو صلاحياته من قناة الطلب."

            # ── عند القبول: منح الرتبة الرسمية فقط ──
            if decision == "ACCEPTED":
                role_title_hint = opening.get("title", "الرتبة المناسبة") if opening else "الرتبة المناسبة"
                if opening and member and interaction.guild:
                    target_role = None
                    if opening.get("grant_role_id"):
                        target_role = interaction.guild.get_role(opening["grant_role_id"])
                    if not target_role:
                        import re
                        t_title = opening.get("title", "").strip()
                        clean_title = re.sub(r'^[^\w\s]+', '', t_title).strip()
                        for role_name in (t_title, clean_title):
                            if role_name:
                                target_role = next(
                                    (role for role in interaction.guild.roles if role.name.casefold() == role_name.casefold()),
                                    None,
                                )
                                if target_role:
                                    break
                    if target_role and app_role and target_role.id == app_role.id:
                        target_role = None

                    if target_role:
                        bot_member = interaction.guild.me
                        try:
                            if (
                                not target_role.is_default()
                                and not target_role.managed
                                and bot_member
                                and target_role < bot_member.top_role
                                and bot_member.guild_permissions.manage_roles
                            ):
                                await member.add_roles(target_role, reason=f"Accepted for recruitment opening #{opening['id']}")
                                role_result += f"\n✅ تم منح الرتبة الرسمية **{target_role.name}** للمتقدم تلقائياً."
                            else:
                                role_result += f"\n⚠️ **تذكير:** يرجى منح {member.mention} الرتبة الرسمية **{target_role.name}** يدوياً (البوت لا يملك الصلاحية الكافية)."
                        except (discord.HTTPException, discord.NotFound, PermissionError):
                            logger.exception("Application accepted, but its configured role could not be assigned")
                            role_result += f"\n⚠️ **تذكير:** تعذّر منح الرتبة الرسمية تلقائياً — يرجى منح {member.mention} **{target_role.name}** يدوياً."
                    else:
                        role_result += f"\n📌 **تذكير:** لا تنسَ منح {member.mention} الرتبة الرسمية **{role_title_hint}** يدوياً."
                else:
                    role_result += f"\n📌 **تذكير:** تعذّر منح الرتبة الرسمية تلقائياً؛ يرجى منح المتقدم **{role_title_hint}** يدوياً."
            elif decision == "REJECTED":
                role_result = f"\n❌ تم رفض الطلب.{role_result}"

            # ── إرسال DM للمتقدم ──
            try:
                applicant_user = await bot.fetch_user(application["user_id"])
                role_title = opening.get("title", "the role") if opening else "the role"
                if decision == "INTERVIEW":
                    dm_message = f"📞 فريق الستاف يريد إجراء مقابلة صوتية معك بخصوص تقديمك لـ **{role_title}** في **{interaction.guild.name}**.\nسيتم التواصل معك قريباً!"
                elif decision == "ACCEPTED":
                    dm_message = f"🎉 تهانينا! تم **قبول** طلبك للانضمام كـ **{role_title}** في **{interaction.guild.name}**!\nمرحباً بك في الفريق! 🎊"
                else:
                    dm_message = f"❌ نأسف لإبلاغك بأنه تم **رفض** طلبك لـ **{role_title}** في **{interaction.guild.name}**.\nيمكنك المحاولة مرة أخرى في المستقبل! 💪"
                await applicant_user.send(dm_message)  # type: ignore[union-attr]
            except (discord.HTTPException, discord.NotFound):
                logger.info("Could not DM applicant %s about decision", application["user_id"])

            if result.get("capacity_reached") and opening:
                await close_announcement(bot, opening)

            # ── الرد على الأدمن/الأونر الذي اتخذ القرار ──
            decision_ar = {"INTERVIEW": "مقابلة صوتية", "ACCEPTED": "قبول", "REJECTED": "رفض"}.get(decision, decision)
            await interaction.followup.send(
                f"✅ تم تحريك الطلب إلى **{decision_ar}**.{role_result}",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Could not review application %s", application_id)
            await interaction.followup.send("Could not save the decision. Please try again.", ephemeral=True)


async def close_announcement(bot: commands.Bot, opening: dict[str, Any]) -> None:
    channel = bot.get_channel(opening["public_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await bot.fetch_channel(opening["public_channel_id"])
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel) or not opening.get("announcement_message_id"):
        return
    try:
        message = await channel.fetch_message(opening["announcement_message_id"])
        embed = message.embeds[0] if message.embeds else discord.Embed(title=opening["title"])
        embed.color = discord.Color.dark_grey()
        embed.set_footer(text=f"Opening #{opening['id']} · Applications closed")
        await message.edit(embed=embed, view=None)
    except discord.HTTPException:
        logger.warning("Could not update announcement for closed opening %s", opening["id"])


class RecruitmentCog(commands.Cog):
    recruitment = app_commands.Group(name="recruit", description="Manage staff recruitment openings")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Restore persistent controls after restart so existing openings and reviews remain usable.
        try:
            for opening in await self.bot.db.get_recruitments_with_status("OPEN"):
                self.bot.add_view(ApplyView(opening["id"]))
            pending = await self.bot.db.get_pending_recruitment_applications()
            for application in pending:
                self.bot.add_view(ApplicationReviewView(application["id"]))
        except Exception:
            logger.exception("Could not restore recruitment controls; check that the recruitment SQL migration was applied")
        if not self.recruitment_expiry_sweeper.is_running():
            self.recruitment_expiry_sweeper.start()

    def cog_unload(self) -> None:
        self.recruitment_expiry_sweeper.cancel()

    @tasks.loop(seconds=60)
    async def recruitment_expiry_sweeper(self) -> None:
        try:
            for opening in await self.bot.db.get_due_recruitments():
                if await self.bot.db.close_recruitment(opening["id"], opening["guild_id"]):
                    await close_announcement(self.bot, opening)
        except Exception:
            logger.exception("Recruitment expiry sweep failed")

    @recruitment_expiry_sweeper.before_loop
    async def before_recruitment_expiry_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    @recruitment.command(name="create", description="Create an opening with channel and role pickers")
    @app_commands.describe(
        public_channel="Where the opening will be announced (optional, picker shown if omitted)",
        review_channel="Private staff review channel (optional, automatically routed to slot 1/2/3 if omitted)",
        grant_role="Optional Discord role to grant when an applicant is accepted",
    )
    async def create_recruitment(
        self,
        interaction: discord.Interaction,
        public_channel: discord.TextChannel | None = None,
        review_channel: discord.TextChannel | None = None,
        grant_role: discord.Role | None = None,
    ) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return

        # If public channel not given, let admin pick from ChannelSelect
        if public_channel is None:
            msg = "📢 **Step 1:** Choose the announcement channel for this recruitment opening:\n*(Applications will be automatically routed to join-requests-1/2/3 based on the role tier)*"
            view = RecruitmentPublishChannelPickerView(self.bot, review_channel=review_channel, grant_role=grant_role)
            await interaction.response.send_message(msg, view=view, ephemeral=True)
            return

        try:
            options = [discord.SelectOption(label=role, value=role) for role in DEFAULT_RECRUITMENT_ROLES]
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            existing = {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}
            for row in configured:
                label = row["label"].strip()
                if label and label.casefold() not in existing and len(options) < 24:
                    options.append(discord.SelectOption(label=label, value=label))
                    existing.add(label.casefold())
            options.append(discord.SelectOption(label="📋 Other / Custom Role", value="__other__", description="Enter a custom English role name"))
            await interaction.response.send_message(
                f"📢 **Selected Announcement Channel:** {public_channel.mention}\n"
                f"**Step 2:** Choose the recruitment role from the list below:",
                view=RecruitmentRoleSelectView(options, public_channel, review_channel, grant_role),
                ephemeral=True,
            )
        except Exception:
            logger.exception("Could not load recruitment role options")
            await interaction.response.send_message("Could not load role options. Check the recruitment database migration.", ephemeral=True)

    @recruitment.command(name="role-add", description="Add a custom role to the recruitment role picker")
    @app_commands.describe(name="Name shown in the role picker")
    async def add_recruitment_role(self, interaction: discord.Interaction, name: str) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can manage role options.", ephemeral=True)
            return
        name = name.strip()
        if not 2 <= len(name) <= 80 or name.casefold() == "other":
            await interaction.response.send_message("Enter a role name of 2–80 characters. 'Other' is reserved.", ephemeral=True)
            return
        try:
            if name.casefold() in {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}:
                await interaction.response.send_message("That role is already included by default.", ephemeral=True)
                return
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            if any(row["label"].casefold() == name.casefold() for row in configured):
                await interaction.response.send_message("That custom role is already in the picker.", ephemeral=True)
                return
            if len(configured) >= 18:
                await interaction.response.send_message("The picker already has its maximum of 18 custom role options.", ephemeral=True)
                return
            await self.bot.db.add_custom_recruitment_role(interaction.guild.id, name, interaction.user.id)
            await interaction.response.send_message(f"Added **{name}** to `/recruit create`.", ephemeral=True)
        except Exception:
            logger.exception("Could not add recruitment role option")
            await interaction.response.send_message("Could not save that role option. Please check the database migration.", ephemeral=True)

    @recruitment.command(name="role-remove", description="Remove a custom role from the recruitment role picker")
    @app_commands.describe(name="Exact name of the custom option")
    async def remove_recruitment_role(self, interaction: discord.Interaction, name: str) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can manage role options.", ephemeral=True)
            return
        try:
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            match = next((row for row in configured if row["label"].casefold() == name.strip().casefold()), None)
            if not match:
                await interaction.response.send_message("Custom role option not found. Default roles cannot be removed.", ephemeral=True)
                return
            await self.bot.db.delete_custom_recruitment_role(interaction.guild.id, match["id"])
            await interaction.response.send_message(f"Removed **{match['label']}** from the picker.", ephemeral=True)
        except Exception:
            logger.exception("Could not remove recruitment role option")
            await interaction.response.send_message("Could not remove that option. Please check the database migration.", ephemeral=True)

    @recruitment.command(name="open", description="Open an application form for a team role")
    @app_commands.describe(title="Role or team", description="What the role involves and eligibility", public_channel="Where to announce the opening", review_channel="Private staff channel for applications", grant_role="Optional Discord role to grant when an applicant is accepted")
    async def open_recruitment(self, interaction: discord.Interaction, title: str, description: str, public_channel: discord.TextChannel, review_channel: discord.TextChannel, grant_role: discord.Role | None = None) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can open recruitment.", ephemeral=True)
            return
        if not interaction.guild or len(title) > 80 or len(description) > 1800:
            await interaction.response.send_message("Use a title under 80 characters and a description under 1,800 characters, in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        opening: dict[str, Any] | None = None
        announcement: discord.Message | None = None
        try:
            perms = public_channel.permissions_for(interaction.guild.me)
            if not perms.send_messages or not perms.embed_links:
                await interaction.followup.send("I need Send Messages and Embed Links in the public channel.", ephemeral=True)
                return
            review_perms = review_channel.permissions_for(interaction.guild.me)
            if not review_perms.view_channel or not review_perms.send_messages or not review_perms.embed_links:
                await interaction.followup.send("I need View Channel, Send Messages, and Embed Links in the staff review channel.", ephemeral=True)
                return
            if grant_role and (grant_role.guild.id != interaction.guild.id or grant_role.is_default() or grant_role.managed
                               or not interaction.guild.me.guild_permissions.manage_roles or grant_role >= interaction.guild.me.top_role):
                await interaction.followup.send("The optional role must be a regular role below the bot's highest role, and the bot needs Manage Roles.", ephemeral=True)
                return
            # Avoid simultaneous openings for the same role (case-insensitive check).
            active = await self.bot.db.get_open_recruitments(interaction.guild.id)
            if any(row["title"].casefold() == title.casefold() for row in active):
                await interaction.followup.send("An opening with this title is already active. Close it before creating another.", ephemeral=True)
                return
            opening = await self.bot.db.create_recruitment({
                "guild_id": interaction.guild.id,
                "title": title.strip(),
                "description": description.strip(),
                "created_by": interaction.user.id,
                "public_channel_id": public_channel.id,
                "review_channel_id": review_channel.id,
                "status": "OPEN",
                "seat_limit": None,
                "closes_at": None,
                "questions": [],
                "grant_role_id": grant_role.id if grant_role else None,
            })
            embed = discord.Embed(title=f"Now accepting applications: {title.strip()}", description=description.strip(), color=discord.Color.green())
            if grant_role:
                embed.add_field(name="Role after acceptance", value=grant_role.mention, inline=False)
            embed.set_footer(text=f"Opening #{opening['id']} · Apply using the button below")
            announcement = await public_channel.send(embed=embed, view=ApplyView(opening["id"]), allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.set_recruitment_message(opening["id"], announcement.id)
            self.bot.add_view(ApplyView(opening["id"]))
            await interaction.followup.send(f"Recruitment is open in {public_channel.mention} (opening #{opening['id']}).", ephemeral=True)
        except Exception:
            logger.exception("Could not open recruitment")
            if opening:
                try:
                    await self.bot.db.close_recruitment(opening["id"], interaction.guild.id)  # type: ignore[union-attr]
                except Exception:
                    logger.exception("Could not roll back incomplete opening %s", opening["id"])
            if announcement:
                try:
                    await announcement.edit(view=None)
                except discord.HTTPException:
                    logger.warning("Could not disable announcement for incomplete opening")
            await interaction.followup.send("Could not open recruitment. Check the bot's channel permissions and database migration.", ephemeral=True)

    @recruitment.command(name="close", description="Close an active application form")
    @app_commands.describe(opening_id="Opening ID shown in the announcement")
    async def close_recruitment(self, interaction: discord.Interaction, opening_id: int) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can close recruitment.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            opening = await self.bot.db.get_recruitment(opening_id)
            if not opening or opening["guild_id"] != interaction.guild.id:
                await interaction.followup.send("Opening not found in this server.", ephemeral=True)
                return
            if not await self.bot.db.close_recruitment(opening_id, interaction.guild.id):
                await interaction.followup.send("This opening is already closed.", ephemeral=True)
                return
            await close_announcement(self.bot, opening)
            await interaction.followup.send(f"Opening #{opening_id} is now closed.", ephemeral=True)
        except Exception:
            logger.exception("Could not close recruitment %s", opening_id)
            await interaction.followup.send("Could not close this opening. Please retry.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecruitmentCog(bot))
