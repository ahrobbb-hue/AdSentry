"""
AdSentry Supabase Database Handler
Provides asynchronous operations for Submissions, Cooldowns, and Reviews.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any
from supabase import create_async_client, AsyncClient

from config import Config

logger = logging.getLogger("AdSentry.SupabaseDB")


class SupabaseDatabase:
    """Manages asynchronous database interactions with Supabase."""

    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        self.url = (url or Config.SUPABASE_URL).strip()
        self.key = (key or Config.get_supabase_key()).strip()
        self.client: AsyncClient | None = None
        self._guild_configs_cache: dict[int, dict[str, Any]] = {}

    async def connect(self) -> None:
        """Initializes the Supabase asynchronous client."""
        if not self.url or not self.key:
            raise ValueError("Supabase URL and API Key must be set in configuration.")
        
        try:
            self.client = await create_async_client(self.url, self.key)
            logger.info("Supabase client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}", exc_info=True)
            raise

    async def get_client(self) -> AsyncClient:
        """Returns the active Supabase client or initializes it."""
        if self.client is None:
            await self.connect()
        assert self.client is not None
        return self.client

    async def check_connection(self) -> bool:
        """Performs a quick heartbeat check to verify Supabase connectivity."""
        try:
            client = await self.get_client()
            await client.table("submissions").select("id").limit(1).execute()
            return True
        except Exception as e:
            logger.warning(f"Supabase connectivity check notice: {e}")
            return False

    async def close(self) -> None:
        """Closes the client connection cleanly."""
        self.client = None
        logger.info("Supabase client connection closed.")

    # -------------------------------------------------------------
    # Server Configurations Operations (Dynamic /ad-setup)
    # -------------------------------------------------------------

    async def get_guild_config(self, guild_id: int) -> dict[str, Any] | None:
        """
        Retrieves the channel configuration for a guild.
        Checks cache first, then fetches from Supabase.
        Falls back to .env if unconfigured in database.
        """
        if guild_id in self._guild_configs_cache:
            return self._guild_configs_cache[guild_id]

        client = await self.get_client()
        try:
            response = await client.table("server_configs").select("*").eq("guild_id", guild_id).limit(1).execute()
            if response.data:
                config = response.data[0]
                self._guild_configs_cache[guild_id] = config
                return config
        except Exception as e:
            logger.warning(f"Could not fetch server_configs for guild {guild_id}: {e}")

        # Fallback to .env values if set
        if Config.STAFF_REVIEW_CHANNEL_ID and Config.ANNOUNCEMENT_CHANNEL_ID:
            fallback = {
                "guild_id": guild_id,
                "announcement_channel_id": Config.ANNOUNCEMENT_CHANNEL_ID,
                "staff_review_channel_id": Config.STAFF_REVIEW_CHANNEL_ID,
            }
            return fallback

        return None

    async def set_guild_config(
        self,
        guild_id: int,
        announcement_channel_id: int,
        staff_review_channel_id: int,
        last_panel_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Saves or updates dynamic server channel settings in Supabase and updates local cache."""
        client = await self.get_client()
        existing = self._guild_configs_cache.get(guild_id) or {}
        panel_id = last_panel_message_id if last_panel_message_id is not None else existing.get("last_panel_message_id")
        payload = {
            "guild_id": guild_id,
            "announcement_channel_id": announcement_channel_id,
            "staff_review_channel_id": staff_review_channel_id,
            "last_panel_message_id": panel_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await client.table("server_configs").upsert(payload).execute()
            self._guild_configs_cache[guild_id] = payload
            logger.info(
                f"Configured guild {guild_id}: Announcement Channel={announcement_channel_id}, "
                f"Review Channel={staff_review_channel_id}, Panel Message ID={panel_id}"
            )
            return payload
        except Exception as e:
            logger.error(f"Failed to save server_configs for guild {guild_id}: {e}", exc_info=True)
            raise

    async def update_panel_message_id(self, guild_id: int, message_id: int | None) -> None:
        """Updates the last active ad submission panel message ID for a guild."""
        client = await self.get_client()
        try:
            config = await self.get_guild_config(guild_id)
            if config:
                await client.table("server_configs").update({
                    "last_panel_message_id": message_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("guild_id", guild_id).execute()

                if guild_id in self._guild_configs_cache:
                    self._guild_configs_cache[guild_id]["last_panel_message_id"] = message_id
            else:
                payload = {
                    "guild_id": guild_id,
                    "announcement_channel_id": 0,
                    "staff_review_channel_id": 0,
                    "last_panel_message_id": message_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                await client.table("server_configs").upsert(payload).execute()
                self._guild_configs_cache[guild_id] = payload

            logger.info(f"Updated last_panel_message_id for guild {guild_id} -> {message_id}")
        except Exception as e:
            logger.error(f"Failed to update last_panel_message_id for guild {guild_id}: {e}", exc_info=True)

    # -------------------------------------------------------------
    # Submissions Operations
    # -------------------------------------------------------------

    async def create_submission(
        self,
        user_id: int,
        user_name: str,
        user_avatar_url: str | None,
        category: str,
        content: str,
        title: str | None = None,
        guild_id: int | None = None,
        language: str = "en",
    ) -> dict[str, Any] | None:
        """Creates a new PENDING ad submission in Supabase."""
        client = await self.get_client()
        payload = {
            "guild_id": guild_id,
            "user_id": user_id,
            "user_name": user_name,
            "user_avatar_url": user_avatar_url,
            "category": category,
            "title": title,
            "content": content,
            "language": language,
            "status": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            try:
                response = await client.table("submissions").insert(payload).execute()
            except Exception as insert_err:
                # Fallback if 'language' column hasn't been migrated yet in Supabase
                if "language" in str(insert_err).lower():
                    logger.warning("Supabase submissions table missing 'language' column. Falling back without it.")
                    payload.pop("language", None)
                    response = await client.table("submissions").insert(payload).execute()
                else:
                    raise insert_err

            if response.data and len(response.data) > 0:
                logger.info(f"Created submission #{response.data[0].get('id')} for User {user_id} (Lang: {language})")
                return response.data[0]
            return None
        except Exception as e:
            logger.error(f"Error inserting submission for User {user_id}: {e}", exc_info=True)
            raise

    async def has_pending_submission(self, user_id: int) -> bool:
        """Checks if a user already has an active PENDING submission."""
        client = await self.get_client()
        try:
            response = (
                await client.table("submissions")
                .select("id")
                .eq("user_id", user_id)
                .eq("status", "PENDING")
                .limit(1)
                .execute()
            )
            return len(response.data) > 0
        except Exception as e:
            logger.error(f"Error checking pending submissions for User {user_id}: {e}", exc_info=True)
            return False

    async def update_review_message_id(self, submission_id: int, message_id: int) -> bool:
        """Updates the staff review channel message ID associated with a submission."""
        client = await self.get_client()
        try:
            await client.table("submissions").update({"review_message_id": message_id}).eq("id", submission_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating review_message_id for submission #{submission_id}: {e}", exc_info=True)
            return False

    # -------------------------------------------------------------
    # User Cooldowns & Anti-Spam Operations
    # -------------------------------------------------------------

    async def get_user_cooldown(self, user_id: int) -> datetime | None:
        """
        Retrieves the cooldown expiration for a user.
        Returns the future datetime if the user is currently on cooldown, otherwise None.
        """
        client = await self.get_client()
        try:
            response = (
                await client.table("user_cooldowns")
                .select("cooldown_until")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            if not response.data:
                return None

            raw_until = response.data[0].get("cooldown_until")
            if not raw_until:
                return None

            # Parse ISO 8601 string to timezone-aware UTC datetime
            cooldown_until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)

            if cooldown_until > now:
                return cooldown_until
            return None
        except Exception as e:
            logger.error(f"Error fetching cooldown for User {user_id}: {e}", exc_info=True)
            return None

    async def set_user_cooldown(self, user_id: int, hours: int | None = None) -> datetime:
        """
        Sets or updates the anti-spam cooldown for a user.
        Calculates cooldown_until = NOW + COOLDOWN_HOURS and upserts the row.
        """
        if hours is None:
            hours = Config.COOLDOWN_HOURS

        client = await self.get_client()
        now = datetime.now(timezone.utc)
        cooldown_until = now + timedelta(hours=hours)

        payload = {
            "user_id": user_id,
            "last_submission_at": now.isoformat(),
            "cooldown_until": cooldown_until.isoformat(),
        }

        try:
            await client.table("user_cooldowns").upsert(payload).execute()
            logger.info(f"Cooldown set for User {user_id} until {cooldown_until.isoformat()}")
            return cooldown_until
        except Exception as e:
            logger.error(f"Error setting cooldown for User {user_id}: {e}", exc_info=True)
            raise

    # -------------------------------------------------------------
    # Review & Moderation Operations
    # -------------------------------------------------------------

    async def get_submission(self, submission_id: int) -> dict[str, Any] | None:
        """Retrieves a single submission by its ID."""
        client = await self.get_client()
        try:
            response = await client.table("submissions").select("*").eq("id", submission_id).limit(1).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"Error fetching submission #{submission_id}: {e}", exc_info=True)
            return None

    async def update_submission_status(
        self,
        submission_id: int,
        status: str,
        reviewed_by: int,
        rejection_reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Updates submission status ('APPROVED' or 'REJECTED') with reviewer info."""
        client = await self.get_client()
        update_payload: dict[str, Any] = {
            "status": status,
            "reviewed_by": reviewed_by,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        if rejection_reason is not None:
            update_payload["rejection_reason"] = rejection_reason

        try:
            response = await client.table("submissions").update(update_payload).eq("id", submission_id).execute()
            logger.info(f"Updated submission #{submission_id} status to {status} by staff {reviewed_by}")
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"Error updating submission #{submission_id} to {status}: {e}", exc_info=True)
            raise

    # -------------------------------------------------------------
    # Active Posts & Auto-Expiry Sweeper Operations
    # -------------------------------------------------------------

    async def create_active_post(
        self,
        submission_id: int,
        channel_id: int,
        message_id: int,
        webhook_url: str | None,
        expires_at: datetime | None,
        thread_id: int | None = None,
        guild_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Registers a newly published announcement post in active_posts."""
        client = await self.get_client()
        payload: dict[str, Any] = {
            "guild_id": guild_id,
            "submission_id": submission_id,
            "channel_id": channel_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "webhook_url": webhook_url,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "is_expired": False,
        }

        try:
            response = await client.table("active_posts").insert(payload).execute()
            logger.info(f"Registered active post for submission #{submission_id} (Expires: {payload['expires_at']})")
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"Error creating active post for submission #{submission_id}: {e}", exc_info=True)
            raise

    async def get_expired_posts(self) -> list[dict[str, Any]]:
        """
        Retrieves all active posts whose expiration timestamp has passed and are not yet marked expired.
        """
        client = await self.get_client()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            response = (
                await client.table("active_posts")
                .select("*")
                .eq("is_expired", False)
                .not_.is_("expires_at", "null")
                .lte("expires_at", now_iso)
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching expired active posts: {e}", exc_info=True)
            return []

    async def mark_post_expired(self, post_id: int) -> bool:
        """Marks an active post as expired in Supabase."""
        client = await self.get_client()
        try:
            await client.table("active_posts").update({"is_expired": True}).eq("id", post_id).execute()
            logger.info(f"Marked active post #{post_id} as expired.")
            return True
        except Exception as e:
            logger.error(f"Error marking active post #{post_id} as expired: {e}", exc_info=True)
            return False

    async def search_active_ads(
        self,
        guild_id: int | None = None,
        category_query: str | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """
        Searches active (non-expired) announcement posts, optionally filtered
        by guild_id and category query (matching Arabic/English category keywords).
        """
        client = await self.get_client()
        try:
            query = (
                client.table("active_posts")
                .select("id, channel_id, message_id, thread_id, expires_at, published_at, submissions(id, user_name, title, category, content, language, user_avatar_url)")
                .eq("is_expired", False)
                .order("published_at", desc=True)
            )
            if guild_id:
                query = query.eq("guild_id", guild_id)

            response = await query.execute()
            posts = response.data or []

            # If category_query is provided and not 'ALL', filter matching posts
            if category_query and category_query.upper() != "ALL":
                tokens = [t.strip().lower() for t in category_query.split("|") if t.strip()]
                filtered = []
                for p in posts:
                    sub = p.get("submissions")
                    if sub and isinstance(sub, dict):
                        cat = (sub.get("category") or "").lower()
                        title = (sub.get("title") or "").lower()
                        if any(token in cat or token in title for token in tokens):
                            filtered.append(p)
                return filtered[:limit]

            return posts[:limit]
        except Exception as e:
            logger.error(f"Error searching active ads: {e}", exc_info=True)
            return []
