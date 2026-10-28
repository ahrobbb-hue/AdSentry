"""
AdSentry Webhook Publisher Utility
Handles creating/retrieving webhooks, posting ads with modern marketing-grade embed cards,
banner images, category-based theming, and spawning discussion threads.
"""

from __future__ import annotations

from typing import Any


import logging
import re
from datetime import datetime, timezone
import discord

logger = logging.getLogger("AdSentry.WebhookPublisher")

# Regex to detect direct image URLs from common hosting services or extensions
IMAGE_URL_PATTERN = re.compile(
    r'(https?://[^\s<>"\'{}|\\^`]+(?:\.(?:png|jpe?g|gif|webp)|cdn\.discordapp\.com/attachments/|media\.discordapp\.net/attachments/|i\.imgur\.com/)[^\s<>"\'{}|\\^`]*)',
    re.IGNORECASE,
)


def resolve_category_theme(category: str) -> dict[str, Any]:
    """Resolves matching color, emoji, and badges for a given category name."""
    cat_lower = (category or "").lower()

    if any(k in cat_lower for k in ("program", "برمج", "dev", "code", "تطوير")):
        return {
            "emoji": "💻",
            "color": discord.Color.from_rgb(0, 210, 106),
            "badge": "💻 البرمجة والتطوير | Programming",
        }
    if any(k in cat_lower for k in ("bot", "بوت", "tool", "أداة", "أدوات")):
        return {
            "emoji": "🤖",
            "color": discord.Color.from_rgb(114, 137, 218),
            "badge": "🤖 بوتات وأدوات | Bots & Tools",
        }
    if any(k in cat_lower for k in ("server", "سيرفر", "مجتمع", "communit")):
        return {
            "emoji": "🌐",
            "color": discord.Color.from_rgb(88, 101, 242),
            "badge": "🌐 سيرفرات ديسكورد | Discord Servers",
        }
    if any(k in cat_lower for k in ("host", "استضافة", "cloud", "سحاب", "vps")):
        return {
            "emoji": "☁️",
            "color": discord.Color.from_rgb(52, 152, 219),
            "badge": "☁️ استضافة وسحابيات | Hosting & Cloud",
        }
    if any(k in cat_lower for k in ("learn", "تعليم", "resource", "مصادر", "course", "كورس")):
        return {
            "emoji": "📚",
            "color": discord.Color.from_rgb(230, 126, 34),
            "badge": "📚 تعليم ومصادر | Learning & Resources",
        }
    if any(k in cat_lower for k in ("job", "وظائف", "توظيف", "hiring", "work", "عمل", "خدم", "service", "freelance", "حر")):
        return {
            "emoji": "💼",
            "color": discord.Color.from_rgb(241, 196, 15),
            "badge": "💼 وظائف وخدمات | Jobs & Services",
        }

    return {
        "emoji": "📌",
        "color": discord.Color.from_rgb(52, 152, 219),
        "badge": f"📌 {category}",
    }


def extract_and_clean_image(content: str, explicit_image: str | None = None) -> tuple[str | None, str]:
    """
    Extracts an image URL (from explicit input or regex in content).
    Returns (image_url, cleaned_content).
    """
    if explicit_image and (explicit_image.startswith("http://") or explicit_image.startswith("https://")):
        return explicit_image.strip(), content

    # Search for embedded image link in the content
    match = IMAGE_URL_PATTERN.search(content)
    if match:
        extracted_url = match.group(1).strip()
        # Clean URL if it's on its own line
        cleaned_content = re.sub(re.escape(extracted_url), "", content).strip()
        return extracted_url, cleaned_content

    return None, content


async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    """Finds an existing AdSentry webhook or creates a new one in the target channel."""
    try:
        webhooks = await channel.webhooks()
        # Prefer a webhook we own (has a token) and is named AdSentry
        for wh in webhooks:
            if wh.token is not None and wh.name.startswith("AdSentry"):
                return wh
        # Fall back to any webhook we own
        for wh in webhooks:
            if wh.token is not None:
                return wh

        webhook = await channel.create_webhook(
            name="AdSentry Publisher",
            reason="AdSentry announcement publishing system",
        )
        logger.info(f"Created new webhook '{webhook.name}' in #{channel.name} (ID: {channel.id})")
        return webhook
    except discord.Forbidden:
        logger.error(f"Missing 'Manage Webhooks' permission in #{channel.name} (ID: {channel.id})")
        raise
    except Exception as e:
        logger.error(f"Failed to get/create webhook in #{channel.name}: {e}", exc_info=True)
        raise


async def publish_ad_via_webhook(
    channel: discord.TextChannel,
    user_name: str,
    user_avatar_url: str | None,
    title: str,
    category: str,
    content: str,
    post_id: int | None = None,
    user_id: int | None = None,
    image_url: str | None = None,
    reviewer_id: int | None = None,
    submission_id: int | None = None,
    tags: list[str] | None = None,
) -> tuple[discord.WebhookMessage, discord.Thread | None, str]:
    """
    Publishes a premium, modern advertisement card via webhook disguised as the submitter.
    Includes banner images, user avatars, structured badges, staff audit, and spawns discussion threads.
    Returns: (published_message, discussion_thread, webhook_url)
    """
    from cogs.features import AdInterestView, TranslateView

    webhook = await get_or_create_webhook(channel)

    # Resolve theme styling
    theme = resolve_category_theme(category)
    emoji = theme["emoji"]
    color = theme["color"]
    badge_label = theme["badge"]

    # Extract & clean image if present
    final_image_url, clean_content = extract_and_clean_image(content, image_url)

    # Build the card
    embed = discord.Embed(
        title=f"{emoji} {title.strip()}",
        description=f"{clean_content}\n\n──────────────────────────────",
        color=color,
    )

    # 1. Author Header with User Badge
    embed.set_author(
        name=f"{user_name}  •  إعلان موثّق | Verified Ad",
        icon_url=user_avatar_url,
    )

    # 2. Thumbnail (submitter avatar) in upper corner
    if user_avatar_url:
        embed.set_thumbnail(url=user_avatar_url)

    # 3. Large Banner Image (if provided)
    if final_image_url:
        embed.set_image(url=final_image_url)

    # 4. Inline Structured Fields
    embed.add_field(
        name="🏷️ القسم | Category",
        value=f"`{badge_label}`",
        inline=True,
    )

    if tags:
        tag_str = " ".join(f"`#{t.lstrip('#')}`" for t in tags[:5])
        embed.add_field(
            name="🔖 الوسوم | Tags",
            value=tag_str,
            inline=True,
        )

    if user_id:
        embed.add_field(
            name="👤 المعلن | Advertiser",
            value=f"<@{user_id}>",
            inline=True,
        )
    else:
        embed.add_field(
            name="👤 المعلن | Advertiser",
            value=f"**{user_name}**",
            inline=True,
        )

    # 5. Official Staff Verification Audit Field
    if reviewer_id:
        embed.add_field(
            name="🛡️ التوثيق والاعتماد | Staff Audit",
            value=f"✅ معتمد رسمياً بواسطة: <@{reviewer_id}>\n`🔖 كود الترخيص: #ADS-{submission_id or 'OFFICIAL'}`",
            inline=False,
        )
    else:
        embed.add_field(
            name="🛡️ الفحص | Status",
            value="`✅ موثّق / Verified`",
            inline=True,
        )

    # 5. Footer & Timestamp
    guild_name = channel.guild.name if channel.guild else "Discord"
    guild_icon = channel.guild.icon.url if (channel.guild and channel.guild.icon) else None
    embed.set_footer(
        text=f"AdSentry Moderation • {guild_name}",
        icon_url=guild_icon,
    )
    embed.timestamp = datetime.now(timezone.utc)

    # Initial persistent view
    _temp_post_id = post_id or 0
    interest_view = AdInterestView(post_id=_temp_post_id, interest_count=0)

    # Send announcement via Webhook
    published_message = await webhook.send(
        embed=embed,
        username=user_name,
        avatar_url=user_avatar_url,
        wait=True,
        view=interest_view,
    )

    # Create associated discussion thread
    thread = None
    try:
        thread_title = f"💬 {title[:70]} - Discussion"
        thread = await published_message.create_thread(
            name=thread_title,
            auto_archive_duration=1440,  # 24 hours
            reason="AdSentry discussion thread for published ad",
        )

        # Welcome message inside thread
        welcome_embed = discord.Embed(
            title="💬 روم النقاش والاستفسارات | Discussion & Inquiries",
            description=(
                f"أهلاً بكم في المساحة المخصصة للتواصل والنقاش حول إعلان **{title}**.\n"
                f"Welcome to the dedicated discussion thread for **{title}**.\n\n"
                f"• يمكنكم طرح الأسئلة والتواصل مع المعلن: {f'<@{user_id}>' if user_id else f'**{user_name}**'}\n"
                "• يرجى الالتزام بالاحترام المتبادل وقوانين السيرفر.\n"
                "• Feel free to ask questions or discuss terms directly here."
            ),
            color=color,
        )
        if user_avatar_url:
            welcome_embed.set_thumbnail(url=user_avatar_url)
        welcome_embed.set_footer(text="AdSentry Thread Manager • يغلق تلقائياً بعد 24 ساعة من عدم التفاعل")

        translate_view = TranslateView(
            post_id=_temp_post_id,
            ad_content=clean_content,
            ad_title=title,
        )
        await thread.send(embed=welcome_embed, view=translate_view)
        logger.info(f"Created discussion thread '{thread.name}' for published ad #{published_message.id}")

        # Update the main message to attach a direct link button to the newly created thread
        try:
            view_with_thread = AdInterestView(
                post_id=_temp_post_id,
                interest_count=0,
                thread_url=thread.jump_url,
            )
            await webhook.edit_message(message_id=published_message.id, view=view_with_thread)
        except Exception as e:
            logger.debug(f"Could not update webhook message with thread link button: {e}")

    except discord.Forbidden:
        logger.warning(f"Bot lacks permission to create threads in #{channel.name}.")
    except Exception as e:
        logger.warning(f"Could not create discussion thread for message {published_message.id}: {e}")

    return published_message, thread, webhook.url
