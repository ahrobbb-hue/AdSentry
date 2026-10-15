"""
AdSentry Webhook Publisher Utility
Handles creating/retrieving webhooks, posting ads as the user, and spawning discussion threads.
"""

from __future__ import annotations

import logging
import discord

logger = logging.getLogger("AdSentry.WebhookPublisher")


async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    """Finds an existing AdSentry webhook or creates a new one in the target channel."""
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name.startswith("AdSentry") or wh.token is not None:
                return wh
        
        # None found, create a new webhook
        webhook = await channel.create_webhook(
            name="AdSentry Publisher",
            reason="AdSentry announcement publishing system",
        )
        logger.info(f"Created new webhook '{webhook.name}' in channel #{channel.name} (ID: {channel.id})")
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
) -> tuple[discord.WebhookMessage, discord.Thread | None, str]:
    """
    Publishes an ad via webhook disguised as the submitter, then initializes a discussion thread.
    Returns: (published_message, discussion_thread, webhook_url)
    """
    webhook = await get_or_create_webhook(channel)

    # Category icons map
    category_emojis = {
        "Programming": "💻",
        "Discord Servers": "🌐",
        "Services": "🚀",
        "Others": "📌",
    }
    emoji = category_emojis.get(category, "📢")

    # Build sleek announcement presentation
    embed = discord.Embed(
        title=f"{emoji} {title}",
        description=content,
        color=discord.Color.from_rgb(41, 128, 185),
    )
    embed.add_field(name="Category", value=f"{emoji} {category}", inline=True)
    embed.set_footer(text="Verified Announcement • AdSentry Moderation")

    published_message = await webhook.send(
        embed=embed,
        username=user_name,
        avatar_url=user_avatar_url,
        wait=True,
    )

    # Create associated discussion thread
    thread = None
    try:
        thread_title = f"💬 {title[:70]} - Discussion"
        thread = await published_message.create_thread(
            name=thread_title,
            auto_archive_duration=1440, # 24 hours
            reason="AdSentry discussion thread for published ad",
        )
        
        welcome_embed = discord.Embed(
            title="💬 Announcement Discussion",
            description=(
                f"This thread is dedicated for inquiries, questions, and discussions regarding **{title}**.\n\n"
                "• Feel free to ask the poster questions.\n"
                "• Keep discussions respectful and in line with server rules."
            ),
            color=discord.Color.dark_theme(),
        )
        welcome_embed.set_footer(text="AdSentry Thread Manager")
        await thread.send(embed=welcome_embed)
        logger.info(f"Created discussion thread '{thread.name}' for published ad #{published_message.id}")
    except discord.Forbidden:
        logger.warning(f"Bot lacks permission to create threads in #{channel.name}.")
    except Exception as e:
        logger.warning(f"Could not create discussion thread for message {published_message.id}: {e}")

    return published_message, thread, webhook.url
