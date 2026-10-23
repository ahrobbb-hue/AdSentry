"""StaffFlow: private staff requests, shift planning and decision history."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import is_staff

logger = logging.getLogger("AdSentry.StaffFlow")


class StaffFlow(commands.GroupCog, group_name="staffflow"):
    """Track internal staff operations with an auditable decision trail."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _private_channel(self, guild_id: int) -> discord.TextChannel | None:
        config = await self.bot.db.get_guild_config(guild_id)
        channel_id = (config or {}).get("staff_review_channel_id")
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _submit_case(self, interaction: discord.Interaction, kind: str, subject: str, details: str, start: str | None = None, end: str | None = None) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        channel = await self._private_channel(interaction.guild.id)
        if not channel:
            await interaction.response.send_message("لم يتم إعداد قناة الإدارة. استخدم `/ad-setup` أولًا.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        client = await self.bot.db.get_client()
        result = await client.table("staff_cases").insert({"guild_id": interaction.guild.id, "user_id": interaction.user.id, "user_name": str(interaction.user), "kind": kind, "subject": subject[:120], "details": details[:1800], "starts_at": start, "ends_at": end, "status": "PENDING"}).execute()
        case = result.data[0]
        embed = discord.Embed(title=f"طلب {kind} · #{case['id']}", description=details[:4000], color=discord.Color.orange())
        embed.add_field(name="الموظف", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="الموضوع", value=subject[:1024], inline=False)
        if start: embed.add_field(name="من", value=start)
        if end: embed.add_field(name="إلى", value=end)
        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await client.table("staff_cases").update({"review_channel_id": channel.id, "review_message_id": message.id}).eq("id", case["id"]).execute()
        except Exception:
            await client.table("staff_cases").update({"status": "DELIVERY_FAILED"}).eq("id", case["id"]).execute()
            logger.exception("Could not deliver staff case %s to its review channel", case["id"])
            await interaction.followup.send("تعذر إيصال الطلب إلى قناة الإدارة. لم يُسجّل كطلب قابل للمراجعة؛ يرجى التواصل مع الإدارة مباشرة.", ephemeral=True)
            return
        await interaction.followup.send(f"تم إرسال طلبك بسرية إلى الإدارة. رقم الطلب: **#{case['id']}**.", ephemeral=True)

    @app_commands.command(name="leave", description="إرسال طلب إجازة للإدارة")
    async def leave(self, interaction: discord.Interaction, start_date: str, end_date: str, reason: str) -> None:
        if not interaction.guild: await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        try:
            start = datetime.fromisoformat(start_date).date(); end = datetime.fromisoformat(end_date).date()
            if end < start or start < datetime.now(timezone.utc).date(): raise ValueError
        except ValueError:
            await interaction.response.send_message("أدخل تاريخين صحيحين بصيغة YYYY-MM-DD، ويجب أن يكون تاريخ النهاية بعد البداية.", ephemeral=True); return
        await self._submit_case(interaction, "إجازة", "طلب إجازة", reason, start.isoformat(), end.isoformat())

    @app_commands.command(name="complaint", description="إرسال شكوى إدارية سرية")
    async def complaint(self, interaction: discord.Interaction, subject: str, details: str) -> None:
        await self._submit_case(interaction, "شكوى", subject, details)

    @app_commands.command(name="review", description="اتخاذ قرار وتسجيله على طلب طاقم")
    @app_commands.choices(decision=[app_commands.Choice(name="قبول", value="APPROVED"), app_commands.Choice(name="رفض", value="REJECTED")])
    async def review(self, interaction: discord.Interaction, case_id: int, decision: app_commands.Choice[str], note: str) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        client = await self.bot.db.get_client()
        result = await client.rpc("review_staff_case", {"p_case_id": case_id, "p_guild_id": interaction.guild.id, "p_reviewer_id": interaction.user.id, "p_decision": decision.value, "p_note": note[:1000]}).execute()
        row = result.data[0] if isinstance(result.data, list) and result.data else result.data
        if not isinstance(row, dict) or not row.get("changed"):
            await interaction.response.send_message("لم أجد طلبًا معلقًا بهذا الرقم، أو سبق اتخاذ قرار بشأنه.", ephemeral=True); return
        case = row["case"]
        channel = self.bot.get_channel(int(case.get("review_channel_id") or 0))
        if isinstance(channel, discord.TextChannel) and case.get("review_message_id"):
            try:
                msg = await channel.fetch_message(int(case["review_message_id"]))
                if msg.embeds:
                    embed = msg.embeds[0]; embed.color = discord.Color.green() if decision.value == "APPROVED" else discord.Color.red()
                    embed.add_field(name="القرار", value=f"{decision.name} بواسطة {interaction.user.mention}\n{note[:900]}", inline=False)
                    await msg.edit(embed=embed)
            except discord.HTTPException:
                logger.warning("Could not update staff case review message %s", case_id)
        member = interaction.guild.get_member(int(case["user_id"]))
        if member:
            try: await member.send(f"تمت مراجعة طلبك رقم **#{case_id}**: **{decision.name}**.\nملاحظة الإدارة: {note[:1000]}")
            except discord.HTTPException: pass
        await interaction.response.send_message(f"تم تسجيل القرار على الطلب #{case_id} في سجل التدقيق.", ephemeral=True)

    @app_commands.command(name="shift-create", description="جدولة مناوبة لموظف")
    async def shift_create(self, interaction: discord.Interaction, member: discord.Member, starts_at: str, duration_hours: app_commands.Range[int, 1, 24], duties: str) -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        try:
            start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(timezone.utc)
            if start <= datetime.now(timezone.utc): raise ValueError
        except ValueError:
            await interaction.response.send_message("أدخل موعدًا مستقبليًا بصيغة ISO.", ephemeral=True); return
        client = await self.bot.db.get_client()
        from datetime import timedelta
        result = await client.table("staff_shifts").insert({"guild_id": interaction.guild.id, "user_id": member.id, "created_by": interaction.user.id, "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=duration_hours)).isoformat(), "duties": duties[:1000], "status": "SCHEDULED"}).execute()
        shift = result.data[0]
        await client.table("staff_audit").insert({"guild_id": interaction.guild.id, "actor_id": interaction.user.id, "target_user_id": member.id, "action": "SHIFT_SCHEDULED", "details": {"shift_id": shift["id"], "starts_at": start.isoformat(), "duration_hours": duration_hours, "duties": duties[:1000]}}).execute()
        await interaction.response.send_message(f"تمت جدولة مناوبة {member.mention} في <t:{int(start.timestamp())}:F>.", ephemeral=True)
        try: await member.send(f"تمت جدولة مناوبتك في **{interaction.guild.name}**: <t:{int(start.timestamp())}:F> لمدة {duration_hours} ساعة.\nالمهام: {duties[:1000]}")
        except discord.HTTPException: pass

    @app_commands.command(name="shift-list", description="عرض المناوبات القادمة")
    async def shift_list(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if not interaction.guild: await interaction.response.send_message("استخدم الأمر داخل السيرفر.", ephemeral=True); return
        if not is_staff(interaction.user) and member and member.id != interaction.user.id:
            await interaction.response.send_message("يمكنك الاطلاع على مناوباتك فقط.", ephemeral=True); return
        target = member.id if member and is_staff(interaction.user) else interaction.user.id
        client = await self.bot.db.get_client()
        rows = (await client.table("staff_shifts").select("*").eq("guild_id", interaction.guild.id).eq("user_id", target).eq("status", "SCHEDULED").order("starts_at").limit(10).execute()).data or []
        if not rows: await interaction.response.send_message("لا توجد مناوبات قادمة.", ephemeral=True); return
        embed = discord.Embed(title=f"مناوبات {interaction.guild.get_member(target) or target}", color=discord.Color.blurple())
        for shift in rows:
            start = datetime.fromisoformat(shift["starts_at"].replace("Z", "+00:00")); end = datetime.fromisoformat(shift["ends_at"].replace("Z", "+00:00"))
            embed.add_field(name=f"#{shift['id']} · <t:{int(start.timestamp())}:f>", value=f"حتى <t:{int(end.timestamp())}:t>\n{shift['duties']}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="promote", description="تسجيل ترقية أو تغيير منصب في سجل الإدارة")
    async def promote(self, interaction: discord.Interaction, member: discord.Member, new_position: str, reason: str, previous_position: str = "غير محدد") -> None:
        if not interaction.guild or not is_staff(interaction.user):
            await interaction.response.send_message("هذا الأمر مخصص للإدارة.", ephemeral=True); return
        client = await self.bot.db.get_client()
        result = await client.table("staff_audit").insert({"guild_id": interaction.guild.id, "actor_id": interaction.user.id, "target_user_id": member.id, "action": "POSITION_CHANGE", "details": {"previous": previous_position[:100], "new": new_position[:100], "reason": reason[:1000]}}).execute()
        await interaction.response.send_message(f"تم توثيق تغيير منصب {member.mention} إلى **{new_position[:100]}** (سجل #{result.data[0]['id']}).", ephemeral=True)
        channel = await self._private_channel(interaction.guild.id)
        if channel:
            await channel.send(embed=discord.Embed(title="تسجيل تغيير منصب", description=f"{member.mention}: **{previous_position[:100]}** ← **{new_position[:100]}**\nالسبب: {reason[:1000]}\nالمسؤول: {interaction.user.mention}", color=discord.Color.gold()), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffFlow(bot))
