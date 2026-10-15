"""
AdSentry Features Cog
Implements: /ad-stats, /my-ads, ⭐ Interest button, 🌐 Translate button.
Anti-phishing is handled in utils/automod.py (screen_content).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import is_staff
from utils import translate_ad, detect_language

logger = logging.getLogger("AdSentry.Features")


# ---------------------------------------------------------------------------
# ⭐ Ad Interest Button (persistent, attached to published ads)
# ---------------------------------------------------------------------------

class AdInterestButton(discord.ui.Button):
    """Persistent ⭐ Interest button attached to published ads."""

    def __init__(self, post_id: int, interest_count: int = 0) -> None:
        super().__init__(
            label=f"⭐ مهتم / Interested ({interest_count})",
            style=discord.ButtonStyle.secondary,
            custom_id=f"adsentry:interest:{post_id}",
            row=0,
        )
        self.post_id = post_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot: commands.Bot = interaction.client  # type: ignore

        # Extract post_id from interaction custom_id if available
        post_id = self.post_id
        if interaction.data and "custom_id" in interaction.data:
            parts = str(interaction.data["custom_id"]).split(":")
            if len(parts) >= 3 and parts[2].isdigit():
                parsed_id = int(parts[2])
                if parsed_id > 0:
                    post_id = parsed_id

        msg_id = interaction.message.id if interaction.message else None
        result = await bot.db.toggle_ad_interest(post_id, interaction.user.id, message_id=msg_id)
        action = result.get("action", "error")
        count = result.get("count", 0)
        resolved_post_id = result.get("post_id", post_id)

        if action == "error":
            await interaction.followup.send(
                "❌ حدث خطأ أثناء تسجيل اهتمامك. حاول مجدداً.\nAn error occurred. Please try again.",
                ephemeral=True,
            )
            return

        if action == "added":
            # Send ad details to user DM
            try:
                client = await bot.db.get_client()
                query = (
                    client.table("active_posts")
                    .select("channel_id, message_id, submissions(title, category, content, user_name)")
                )
                if resolved_post_id and resolved_post_id > 0:
                    post_res = await query.eq("id", resolved_post_id).limit(1).execute()
                elif msg_id:
                    post_res = await query.eq("message_id", msg_id).limit(1).execute()
                else:
                    post_res = None

                if post_res and post_res.data:
                    post = post_res.data[0]
                    sub = post.get("submissions") or {}
                    title = sub.get("title") or "إعلان"
                    category = sub.get("category") or ""
                    content = (sub.get("content") or "")[:500]
                    author = sub.get("user_name") or "Anonymous"
                    channel_id = post.get("channel_id")
                    message_id = post.get("message_id")
                    jump_url = f"https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id}"

                    dm_embed = discord.Embed(
                        title=f"⭐ إعلان محفوظ | Saved Ad: {title}",
                        description=f"{content}\n\n🔗 **[عرض الإعلان الكامل / View Full Ad]({jump_url})**",
                        color=discord.Color.gold(),
                    )
                    dm_embed.add_field(name="👤 الناشر | Author", value=author, inline=True)
                    dm_embed.add_field(name="🏷️ التصنيف | Category", value=category, inline=True)
                    dm_embed.set_footer(text="AdSentry • تم حفظ هذا الإعلان في رسائلك الخاصة")
                    await interaction.user.send(embed=dm_embed)
            except discord.Forbidden:
                pass  # DMs closed
            except Exception as e:
                logger.warning(f"Failed to send interest DM: {e}")

            await interaction.followup.send(
                f"⭐ **تم حفظ الإعلان في رسائلك الخاصة!**\nAd saved to your DMs! Total interested: **{count}**",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ **تم إلغاء اهتمامك بهذا الإعلان.**\nInterest removed. Total interested: **{count}**",
                ephemeral=True,
            )

        # Update button label on the message
        try:
            view = discord.ui.View.from_message(interaction.message)  # type: ignore
            for item in view.children:
                if isinstance(item, discord.ui.Button) and (
                    item.custom_id == f"adsentry:interest:{self.post_id}" or
                    item.custom_id == f"adsentry:interest:{resolved_post_id}" or
                    (item.custom_id and item.custom_id.startswith("adsentry:interest:"))
                ):
                    item.label = f"⭐ مهتم / Interested ({count})"
                    item.custom_id = f"adsentry:interest:{resolved_post_id}"
            await interaction.message.edit(view=view)  # type: ignore
        except Exception:
            pass  # Non-critical


class AdVerifyButton(discord.ui.Button):
    """Persistent button that checks live database audit records to confirm staff approval."""

    def __init__(self, post_id: int = 0) -> None:
        super().__init__(
            label="🛡️ فحص التوثيق | Verify",
            style=discord.ButtonStyle.secondary,
            custom_id=f"adsentry:verify:{post_id}",
            row=0,
        )
        self.post_id = post_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot: commands.Bot = interaction.client  # type: ignore

        # Extract post_id from custom_id if available
        post_id = self.post_id
        if interaction.data and "custom_id" in interaction.data:
            parts = str(interaction.data["custom_id"]).split(":")
            if len(parts) >= 3 and parts[2].isdigit():
                parsed_id = int(parts[2])
                if parsed_id > 0:
                    post_id = parsed_id

        msg_id = interaction.message.id if interaction.message else None

        client = await bot.db.get_client()
        post_data = None
        try:
            query = (
                client.table("active_posts")
                .select("id, published_at, submissions(id, title, category, status, reviewed_by, reviewed_at, created_at, user_id, user_name)")
            )
            if post_id and post_id > 0:
                res = await query.eq("id", post_id).limit(1).execute()
                if res.data:
                    post_data = res.data[0]

            if not post_data and msg_id:
                res = await query.eq("message_id", msg_id).limit(1).execute()
                if res.data:
                    post_data = res.data[0]
        except Exception as e:
            logger.error(f"Error checking ad verification audit: {e}", exc_info=True)

        sub = (post_data.get("submissions") if post_data else None) or {}
        is_approved = sub.get("status") == "APPROVED"

        if post_data and is_approved:
            sub_id = sub.get("id") or "N/A"
            reviewer_id = sub.get("reviewed_by")
            reviewed_at = sub.get("reviewed_at") or post_data.get("published_at")
            title = sub.get("title") or "إعلان"
            submitter_id = sub.get("user_id")

            # Parse reviewed_at to unix timestamp
            reviewed_ts_str = "غير محدد"
            if reviewed_at:
                try:
                    dt = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
                    reviewed_ts_str = f"<t:{int(dt.timestamp())}:F> (<t:{int(dt.timestamp())}:R>)"
                except Exception:
                    reviewed_ts_str = str(reviewed_at)[:19]

            # Generate deterministic security hash
            hash_seed = f"{sub_id}-{reviewer_id}-{sub.get('created_at')}"
            audit_hash = hashlib.sha256(hash_seed.encode()).hexdigest()[:12].upper()

            reviewer_text = f"<@{reviewer_id}>" if reviewer_id else "مشرف معتمد (Staff)"
            submitter_text = f"<@{submitter_id}>" if submitter_id else sub.get("user_name", "Unknown")

            embed = discord.Embed(
                title="🛡️ شهادة التدقيق والتوثيق الأمني | Official Security Audit",
                description=(
                    "✅ **إعلان موثّق ومصرّح به رسمياً من إدارة السيرفر بنسبة 100%!**\n"
                    "تم فحص سجلات النظام والتأكد من مراجعة واعتماد هذا الإعلان نظامياً.\n\n"
                    f"• **عنوان الإعلان:** {title}\n"
                    f"• **صاحب الإعلان:** {submitter_text}\n"
                    f"• **المشرف المسؤول عن الاعتماد:** {reviewer_text}\n"
                    f"• **تاريخ ووقت الاعتماد:** {reviewed_ts_str}\n"
                    f"• **رقم رخصة الإعلان:** `ADS-{sub_id}`\n"
                    f"• **البصمة الرقمية للتحقق:** `SHA256-{audit_hash}`\n\n"
                    "🔒 **سلامة المحتوى:** تم اجتياز فحص الروابط المشبوهة وحماية الأعضاء ضد الاحتيال."
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text="AdSentry Security Protocol • نظام الحماية والتوثيق المعتمد")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="🚨 تحذير أمني: إعلان غير موثّق! | Security Warning",
                description=(
                    "⚠️ **تنبيه: لم يتم العثور على ترخيص رسمي معتمد لهذا الإعلان في سجلات النظام!**\n\n"
                    "قد يكون هذا الإعلان نُشر بطريقة غير مصرّح بها أو تم حذفه/إلغاؤه من قبل الإدارة.\n\n"
                    "🔴 **إرشادات الأمان:**\n"
                    "• لا تقم بالضغط على أي روابط خارجية داخل الإعلان.\n"
                    "• لا تقم بإرسال أي معلومات شخصية أو مالية.\n"
                    "• يرجى إبلاغ طاقم إدارة السيرفر فوراً لاتخاذ الإجراءات اللازمة."
                ),
                color=discord.Color.red(),
            )
            embed.set_footer(text="AdSentry Security Alert • إشعار أمني لحماية الأعضاء")
            await interaction.followup.send(embed=embed, ephemeral=True)


class AdInterestView(discord.ui.View):
    """Persistent view containing ⭐ interest, 🛡️ verify audit, and optional thread link."""

    def __init__(self, post_id: int, interest_count: int = 0, thread_url: str | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(AdInterestButton(post_id, interest_count))
        self.add_item(AdVerifyButton(post_id))
        if thread_url:
            self.add_item(
                discord.ui.Button(
                    label="💬 روم النقاش / Discussion",
                    url=thread_url,
                    style=discord.ButtonStyle.link,
                    row=0,
                )
            )


# ---------------------------------------------------------------------------
# 🌐 Translate Button (attached inside ad discussion thread)
# ---------------------------------------------------------------------------

class TranslateButton(discord.ui.Button):
    """Button to translate the ad content to the opposite language."""

    def __init__(self, post_id: int, ad_content: str, ad_title: str) -> None:
        super().__init__(
            label="🌐 ترجمة / Translate",
            style=discord.ButtonStyle.secondary,
            custom_id=f"adsentry:translate:{post_id}",
            row=0,
        )
        self.post_id = post_id
        self.ad_content = ad_content
        self.ad_title = ad_title

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        content = self.ad_content
        title = self.ad_title

        # Restore content dynamically if empty (e.g. after bot restart)
        if not content:
            bot: commands.Bot = interaction.client  # type: ignore
            try:
                client = await bot.db.get_client()
                if interaction.channel_id:
                    lookup = (
                        await client.table("active_posts")
                        .select("submissions(title, content)")
                        .eq("thread_id", interaction.channel_id)
                        .limit(1)
                        .execute()
                    )
                    if lookup.data and lookup.data[0].get("submissions"):
                        sub = lookup.data[0]["submissions"]
                        content = sub.get("content") or ""
                        title = sub.get("title") or ""
            except Exception as e:
                logger.warning(f"Could not restore ad content from DB for translation: {e}")

        # Fallback to starter message in thread
        if not content and isinstance(interaction.channel, discord.Thread):
            try:
                starter = await interaction.channel.fetch_message(interaction.channel.id)
                if starter and starter.embeds:
                    content = starter.embeds[0].description or ""
                    title = starter.embeds[0].title or ""
            except Exception:
                pass

        if not content:
            await interaction.followup.send(
                "❌ **تعذر العثور على نص الإعلان لترجمته.**\nCould not find ad content to translate.",
                ephemeral=True,
            )
            return

        # Detect source language and set target
        src_lang = detect_language(content)
        target_lang = "en" if src_lang == "ar" else "ar"
        lang_label = "Arabic | العربية 🇸🇦" if target_lang == "ar" else "English 🇬🇧"

        await interaction.followup.send(
            f"🌐 **جاري الترجمة إلى {lang_label}...**\nTranslating, please wait...",
            ephemeral=True,
        )

        full_text = f"{title}\n\n{content}".strip()
        translated = await translate_ad(full_text, target_lang)

        if not translated:
            await interaction.followup.send(
                "❌ **فشلت الترجمة التلقائية.**\nAutomatic translation failed. Please try again later.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🌐 ترجمة الإعلان | Ad Translation → {lang_label}",
            description=translated[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="AdSentry AI Translation • Powered by Gemini Flash / Google Translate")
        await interaction.followup.send(embed=embed, ephemeral=True)


class TranslateView(discord.ui.View):
    """Persistent view with translate button for ad threads."""

    def __init__(self, post_id: int, ad_content: str, ad_title: str) -> None:
        super().__init__(timeout=None)
        self.add_item(TranslateButton(post_id, ad_content, ad_title))


# ---------------------------------------------------------------------------
# /my-ads - Self-Service Ad Deletion View
# ---------------------------------------------------------------------------

class MyAdDeleteButton(discord.ui.Button):
    """Button to delete a specific active ad from the server."""

    def __init__(self, post: dict[str, Any], guild_id: int) -> None:
        self.post_data = post
        self.guild_id = guild_id
        super().__init__(
            label=f"🗑️ حذف | Delete: {(post.get('title') or 'Ad')[:25]}",
            style=discord.ButtonStyle.danger,
            custom_id=f"adsentry:myads_delete:{post['id']}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot: commands.Bot = interaction.client  # type: ignore
        post = self.post_data

        channel_id = post.get("channel_id")
        message_id = post.get("message_id")
        thread_id = post.get("thread_id")
        post_id = post["id"]

        # Delete Discord message
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(message_id)
                await msg.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f"Could not delete ad message #{message_id}: {e}")

        # Delete associated thread
        if thread_id:
            try:
                thread = await bot.fetch_channel(thread_id)
                if isinstance(thread, discord.Thread):
                    await thread.delete()
            except Exception:
                pass

        # Mark expired in DB
        await bot.db.delete_active_post(post_id)

        await interaction.followup.send(
            f"✅ **تم حذف إعلانك بنجاح!**\nYour ad **\"{post.get('title', 'Ad')}\"** has been removed from the server.",
            ephemeral=True,
        )

        # Disable this button
        self.disabled = True
        self.label = "✅ تم الحذف | Deleted"
        self.style = discord.ButtonStyle.secondary
        try:
            await interaction.message.edit(view=self.view)  # type: ignore
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Features Cog
# ---------------------------------------------------------------------------

class FeaturesCog(commands.Cog, name="Features"):
    """Cog providing /ad-stats, /my-ads, ⭐ interest, and 🌐 translate features."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -----------------------------------------------------------------------
    # /ad-stats
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="ad-stats",
        description="📊 عرض إحصائيات الإعلانات للسيرفر (للإدارة فقط) | Ad statistics report (Staff only)",
    )
    @app_commands.describe(days="Number of days to include in the report (default: 7)")
    @app_commands.default_permissions(manage_guild=True)
    async def ad_stats(self, interaction: discord.Interaction, days: int = 7) -> None:
        """Shows an admin statistics dashboard for the server's ad activity."""
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ هذا الأمر مخصص للإدارة والمشرفين فقط.\nStaff/Admin permissions required.",
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        days = max(1, min(days, 90))  # clamp between 1-90

        stats = await self.bot.db.get_ad_stats(interaction.guild.id, days=days)

        approved = stats["approved_count"]
        rejected = stats["rejected_count"]
        total = stats["total_count"]
        pending = total - approved - rejected
        approval_rate = round((approved / total * 100) if total > 0 else 0, 1)

        embed = discord.Embed(
            title=f"📊 إحصائيات الإعلانات | Ad Statistics — آخر {days} يوم",
            color=discord.Color.from_rgb(88, 101, 242),
        )
        embed.add_field(
            name="📈 نظرة عامة | Overview",
            value=(
                f"📋 **إجمالي الطلبات / Total:** `{total}`\n"
                f"✅ **مقبولة / Approved:** `{approved}`\n"
                f"❌ **مرفوضة / Rejected:** `{rejected}`\n"
                f"⏳ **معلقة / Pending:** `{pending}`\n"
                f"📊 **نسبة القبول / Approval Rate:** `{approval_rate}%`"
            ),
            inline=False,
        )

        # Top categories
        if stats["top_categories"]:
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            cat_lines = "\n".join(
                f"{medals[i]} `{cat}` — **{count}** طلب"
                for i, (cat, count) in enumerate(stats["top_categories"])
            )
            embed.add_field(
                name="🏆 أكثر التصنيفات طلباً | Top Categories",
                value=cat_lines,
                inline=False,
            )
        else:
            embed.add_field(
                name="🏆 التصنيفات | Categories",
                value="لا توجد بيانات كافية بعد.\nNo data available yet.",
                inline=False,
            )

        # Top reviewers
        if stats["top_reviewers"]:
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            reviewer_lines = "\n".join(
                f"{medals[i]} <@{rev_id}> — **{count}** مراجعة"
                for i, (rev_id, count) in enumerate(stats["top_reviewers"])
            )
            embed.add_field(
                name="🛡️ أكثر المشرفين تفاعلاً | Top Reviewers",
                value=reviewer_lines,
                inline=False,
            )

        embed.set_footer(text=f"AdSentry Analytics • {interaction.guild.name}")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -----------------------------------------------------------------------
    # /my-ads
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="my-ads",
        description="📋 عرض وإدارة إعلاناتك النشطة في السيرفر | View and manage your active ads",
    )
    async def my_ads(self, interaction: discord.Interaction) -> None:
        """Shows the user their active ads with self-delete buttons."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ads = await self.bot.db.get_user_active_ads(interaction.user.id, interaction.guild.id)

        if not ads:
            embed = discord.Embed(
                title="📋 إعلاناتي النشطة | My Active Ads",
                description=(
                    "**لا توجد لديك إعلانات نشطة حالياً في هذا السيرفر.**\n"
                    "You have no active advertisements in this server.\n\n"
                    "💡 استخدم اللوحة الرئيسية لتقديم إعلانك الجديد!"
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 إعلاناتي النشطة | My Active Ads ({len(ads)})",
            description="إعلاناتك النشطة الحالية في السيرفر. يمكنك حذف أي إعلان مبكراً باستخدام الزر أدناه.",
            color=discord.Color.blurple(),
        )

        view = discord.ui.View(timeout=300)
        for i, ad in enumerate(ads[:5], 1):  # max 5 buttons
            title = ad.get("title") or "Ad"
            category = ad.get("category") or ""
            channel_id = ad.get("channel_id")
            message_id = ad.get("message_id")
            expires_at_raw = ad.get("expires_at")
            jump_url = f"https://discord.com/channels/{interaction.guild.id}/{channel_id}/{message_id}"

            if expires_at_raw:
                try:
                    dt = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                    ts = int(dt.timestamp())
                    expiry_str = f"<t:{ts}:F> (<t:{ts}:R>)"
                except Exception:
                    expiry_str = "غير محدد"
            else:
                expiry_str = "دائم (Permanent ♾️)"

            embed.add_field(
                name=f"{i}. {title}",
                value=(
                    f"🏷️ **القسم:** `{category}`\n"
                    f"⏳ **تنتهي:** {expiry_str}\n"
                    f"🔗 **[انتقل للإعلان]({jump_url})**"
                ),
                inline=False,
            )
            btn = MyAdDeleteButton(ad, interaction.guild.id)
            view.add_item(btn)

        embed.set_footer(text="AdSentry • /my-ads — لوحة إعلاناتي")
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Features cog."""
    await bot.add_cog(FeaturesCog(bot))
