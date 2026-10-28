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
        # Track when each guild config was last fetched for TTL-based cache invalidation
        self._guild_configs_fetched_at: dict[int, datetime] = {}
        # Cache TTL: re-fetch from DB after 5 minutes to pick up external changes
        _GUILD_CONFIG_TTL_SECONDS = 300

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
    # Recruitment operations
    # -------------------------------------------------------------

    async def create_recruitment(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self.get_client()
        response = await client.table("recruitments").insert(payload).execute()
        if not response.data:
            raise RuntimeError("Recruitment could not be created.")
        return response.data[0]

    async def get_recruitment(self, recruitment_id: int) -> dict[str, Any] | None:
        client = await self.get_client()
        response = await client.table("recruitments").select("*").eq("id", recruitment_id).limit(1).execute()
        return response.data[0] if response.data else None

    async def get_open_recruitments(self, guild_id: int) -> list[dict[str, Any]]:
        client = await self.get_client()
        response = await client.table("recruitments").select("*").eq("guild_id", guild_id).eq("status", "OPEN").execute()
        return response.data or []

    async def get_custom_recruitment_roles(self, guild_id: int) -> list[dict[str, Any]]:
        client = await self.get_client()
        response = await client.table("recruitment_role_options").select("id, label").eq("guild_id", guild_id).order("label").execute()
        return response.data or []

    async def add_custom_recruitment_role(self, guild_id: int, label: str, added_by: int) -> dict[str, Any]:
        client = await self.get_client()
        response = await client.table("recruitment_role_options").insert({
            "guild_id": guild_id,
            "label": label,
            "added_by": added_by,
        }).execute()
        if not response.data:
            raise RuntimeError("Role option could not be saved.")
        return response.data[0]

    async def delete_custom_recruitment_role(self, guild_id: int, role_id: int) -> bool:
        client = await self.get_client()
        response = await client.table("recruitment_role_options").delete().eq("guild_id", guild_id).eq("id", role_id).execute()
        return bool(response.data)

    async def get_recruitments_with_status(self, status: str) -> list[dict[str, Any]]:
        client = await self.get_client()
        response = await client.table("recruitments").select("*").eq("status", status).execute()
        return response.data or []

    async def get_due_recruitments(self) -> list[dict[str, Any]]:
        client = await self.get_client()
        now = datetime.now(timezone.utc).isoformat()
        response = await client.table("recruitments").select("*").eq("status", "OPEN").not_.is_("closes_at", "null").lte("closes_at", now).execute()
        return response.data or []

    async def get_pending_recruitment_applications(self) -> list[dict[str, Any]]:
        client = await self.get_client()
        response = await client.table("recruitment_applications").select("id").in_("status", ["PENDING", "INTERVIEW"]).execute()
        return response.data or []

    async def set_recruitment_message(self, recruitment_id: int, message_id: int) -> None:
        client = await self.get_client()
        await client.table("recruitments").update({"announcement_message_id": message_id}).eq("id", recruitment_id).execute()

    async def close_recruitment(self, recruitment_id: int, guild_id: int) -> bool:
        client = await self.get_client()
        response = await client.table("recruitments").update({"status": "CLOSED", "closed_at": datetime.now(timezone.utc).isoformat()}).eq("id", recruitment_id).eq("guild_id", guild_id).eq("status", "OPEN").execute()
        return bool(response.data)

    async def create_recruitment_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self.get_client()
        response = await client.rpc("submit_recruitment_application", {
            "p_recruitment_id": payload["recruitment_id"],
            "p_guild_id": payload["guild_id"],
            "p_user_id": payload["user_id"],
            "p_user_name": payload["user_name"],
            "p_experience": payload["experience"],
            "p_motivation": payload["motivation"],
            "p_availability": payload["availability"],
            "p_custom_answers": payload.get("custom_answers", []),
        }).execute()
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise RuntimeError("Application could not be saved.")
        if not data.get("created"):
            return {"id": 0, "reason": data.get("reason", "unknown")}
        return data["application"]

    async def get_recruitment_application(self, application_id: int, guild_id: int) -> dict[str, Any] | None:
        client = await self.get_client()
        response = await client.table("recruitment_applications").select("*").eq("id", application_id).eq("guild_id", guild_id).limit(1).execute()
        return response.data[0] if response.data else None

    async def review_recruitment_application(self, application_id: int, reviewer_id: int, status: str, guild_id: int) -> dict[str, Any]:
        client = await self.get_client()
        response = await client.rpc("review_recruitment_application", {
            "p_application_id": application_id,
            "p_guild_id": guild_id,
            "p_reviewer_id": reviewer_id,
            "p_status": status,
        }).execute()
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else {"changed": False, "reason": "invalid_response"}

    # -------------------------------------------------------------
    # Server Configurations Operations (Dynamic /ad-setup)
    # -------------------------------------------------------------

    async def get_guild_config(self, guild_id: int) -> dict[str, Any] | None:
        """
        Retrieves the channel configuration for a guild.
        Checks cache first (TTL: 5 minutes), then fetches from Supabase.
        Falls back to .env if unconfigured in database.
        """
        CACHE_TTL = 300  # seconds
        now = datetime.now(timezone.utc)
        cached_at = self._guild_configs_fetched_at.get(guild_id)
        if (
            guild_id in self._guild_configs_cache
            and cached_at is not None
            and (now - cached_at).total_seconds() < CACHE_TTL
        ):
            return self._guild_configs_cache[guild_id]

        client = await self.get_client()
        try:
            response = await client.table("server_configs").select("*").eq("guild_id", guild_id).limit(1).execute()
            if response.data:
                config = response.data[0]
                self._guild_configs_cache[guild_id] = config
                self._guild_configs_fetched_at[guild_id] = now
                return config
        except Exception as e:
            logger.warning(f"Could not fetch server_configs for guild {guild_id}: {e}")
            # Return stale cache on DB error rather than breaking
            if guild_id in self._guild_configs_cache:
                return self._guild_configs_cache[guild_id]

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
            # Always refresh the full row from DB to pick up any columns set by other means
            refreshed = await client.table("server_configs").select("*").eq("guild_id", guild_id).limit(1).execute()
            if refreshed.data:
                self._guild_configs_cache[guild_id] = refreshed.data[0]
            else:
                self._guild_configs_cache[guild_id] = payload
            logger.info(
                f"Configured guild {guild_id}: Announcement Channel={announcement_channel_id}, "
                f"Review Channel={staff_review_channel_id}, Panel Message ID={panel_id}"
            )
            return self._guild_configs_cache[guild_id]
        except Exception as e:
            logger.error(f"Failed to save server_configs for guild {guild_id}: {e}", exc_info=True)
            raise

    async def set_staffflow_review_channel(self, guild_id: int, channel_id: int) -> None:
        """Persist the private destination used for StaffFlow cases."""
        client = await self.get_client()
        response = await client.table("server_configs").update({
            "staffflow_review_channel_id": channel_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("guild_id", guild_id).execute()
        if not response.data:
            raise RuntimeError("Guild configuration is missing; run /setup before configuring StaffFlow.")
        config = self._guild_configs_cache.get(guild_id)
        if config is not None:
            config["staffflow_review_channel_id"] = channel_id

    async def set_staffflow_complaint_channel(self, guild_id: int, channel_id: int) -> None:
        """Persist the private channel used only for StaffFlow complaints."""
        client = await self.get_client()
        response = await client.table("server_configs").update({
            "staffflow_complaint_channel_id": channel_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("guild_id", guild_id).execute()
        if not response.data:
            raise RuntimeError("Guild configuration is missing; run /setup before configuring StaffFlow.")
        config = self._guild_configs_cache.get(guild_id)
        if config is not None:
            config["staffflow_complaint_channel_id"] = channel_id

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
        image_url: str | None = None,
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
        if image_url:
            payload["image_url"] = image_url

        try:
            try:
                response = await client.table("submissions").insert(payload).execute()
            except Exception as insert_err:
                err_str = str(insert_err).lower()
                modified = False
                if "image_url" in err_str:
                    logger.warning("Supabase submissions table missing 'image_url' column. Falling back without it.")
                    payload.pop("image_url", None)
                    modified = True
                if "language" in err_str:
                    logger.warning("Supabase submissions table missing 'language' column. Falling back without it.")
                    payload.pop("language", None)
                    modified = True
                if modified:
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

    async def reset_user_cooldown(self, user_id: int) -> bool:
        """
        Resets (removes/clears) the anti-spam cooldown for a user.
        Allows the user to submit advertisements immediately.
        """
        client = await self.get_client()
        try:
            await client.table("user_cooldowns").delete().eq("user_id", user_id).execute()
            logger.info(f"Cooldown successfully reset for User {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error resetting cooldown for User {user_id}: {e}", exc_info=True)
            return False

    async def cancel_pending_submissions(self, user_id: int) -> int:
        """
        Cancels any PENDING submissions for a user so they are not blocked by duplicate submission checks.
        Returns the count of cancelled submissions.
        """
        client = await self.get_client()
        try:
            res = (
                await client.table("submissions")
                .update({
                    "status": "REJECTED",
                    "rejection_reason": "Cleared by Staff/Admin (Cooldown Reset)",
                })
                .eq("user_id", user_id)
                .eq("status", "PENDING")
                .execute()
            )
            count = len(res.data) if res.data else 0
            logger.info(f"Cancelled {count} pending submission(s) for User {user_id}")
            return count
        except Exception as e:
            logger.error(f"Error cancelling pending submissions for User {user_id}: {e}", exc_info=True)
            return 0

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
            # Note: We omit 'language' from submissions(...) as some Supabase schemas
            # do not have that column migrated yet.
            query = (
                client.table("active_posts")
                .select("id, guild_id, channel_id, message_id, thread_id, expires_at, published_at, submissions(id, user_name, title, category, content, user_avatar_url)")
                .eq("is_expired", False)
                .order("published_at", desc=True)
            )
            if guild_id:
                query = query.or_(f"guild_id.eq.{guild_id},guild_id.is.null")

            response = await query.execute()
            posts = response.data or []

            # If category_query is provided and not 'ALL', filter matching posts
            if category_query and category_query.upper() != "ALL":
                def norm(t: str) -> str:
                    t = (t or "").lower()
                    for c_from, c_to in [("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")]:
                        t = t.replace(c_from, c_to)
                    return t

                tokens = [norm(t.strip()) for t in category_query.split("|") if t.strip()]
                filtered = []
                for p in posts:
                    sub = p.get("submissions")
                    if sub and isinstance(sub, dict):
                        cat = norm(sub.get("category") or "")
                        title = norm(sub.get("title") or "")
                        if any(token in cat or token in title for token in tokens):
                            filtered.append(p)
                return filtered[:limit]

            return posts[:limit]
        except Exception as e:
            logger.error(f"Error searching active ads: {e}", exc_info=True)
            return []

    # -------------------------------------------------------------
    # Ad Statistics (for /ad-stats command)
    # -------------------------------------------------------------

    async def get_ad_stats(self, guild_id: int, days: int = 7) -> dict[str, Any]:
        """
        Returns admin stats for a guild covering the past N days:
        - approved_count, rejected_count
        - top_categories (list of (category, count))
        - top_reviewers (list of (reviewer_id, count))
        """
        client = await self.get_client()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            # Fetch all submissions in the window for this guild
            response = (
                await client.table("submissions")
                .select("status, category, reviewed_by")
                .eq("guild_id", guild_id)
                .gte("created_at", cutoff)
                .execute()
            )
            rows = response.data or []

            approved = [r for r in rows if r.get("status") == "APPROVED"]
            rejected = [r for r in rows if r.get("status") == "REJECTED"]

            # Count categories
            cat_counts: dict[str, int] = {}
            for r in rows:
                cat = (r.get("category") or "Other").split(" ")[0]  # strip emoji suffix
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
            top_categories = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:5]

            # Count reviewer activity
            reviewer_counts: dict[str, int] = {}
            for r in rows:
                rev = r.get("reviewed_by")
                if rev:
                    rev_str = str(rev)
                    reviewer_counts[rev_str] = reviewer_counts.get(rev_str, 0) + 1
            top_reviewers = sorted(reviewer_counts.items(), key=lambda x: x[1], reverse=True)[:5]

            return {
                "approved_count": len(approved),
                "rejected_count": len(rejected),
                "total_count": len(rows),
                "top_categories": top_categories,
                "top_reviewers": top_reviewers,
                "days": days,
            }
        except Exception as e:
            logger.error(f"Error fetching ad stats for guild {guild_id}: {e}", exc_info=True)
            return {"approved_count": 0, "rejected_count": 0, "total_count": 0,
                    "top_categories": [], "top_reviewers": [], "days": days}

    # -------------------------------------------------------------
    # User Active Ads (for /my-ads command)
    # -------------------------------------------------------------

    async def get_user_active_ads(self, user_id: int, guild_id: int) -> list[dict[str, Any]]:
        """Fetches all active (non-expired) posts belonging to a specific user in a guild."""
        client = await self.get_client()
        try:
            # Get all approved submissions for this user in this guild
            sub_res = (
                await client.table("submissions")
                .select("id, title, category")
                .eq("user_id", user_id)
                .eq("guild_id", guild_id)
                .eq("status", "APPROVED")
                .execute()
            )
            if not sub_res.data:
                return []

            sub_ids = [s["id"] for s in sub_res.data]
            sub_map = {s["id"]: s for s in sub_res.data}

            # Get active_posts matching those submission IDs
            posts_res = (
                await client.table("active_posts")
                .select("id, submission_id, channel_id, message_id, thread_id, expires_at, published_at")
                .in_("submission_id", sub_ids)
                .eq("is_expired", False)
                .order("published_at", desc=True)
                .execute()
            )
            posts = posts_res.data or []

            # Merge submission metadata into each post
            for p in posts:
                sub_info = sub_map.get(p.get("submission_id"), {})
                p["title"] = sub_info.get("title", "Ad")
                p["category"] = sub_info.get("category", "")

            return posts
        except Exception as e:
            logger.error(f"Error fetching active ads for user {user_id}: {e}", exc_info=True)
            return []

    async def delete_active_post(self, post_id: int) -> bool:
        """Marks an active post as expired (soft delete) so statistics are preserved.
        Used when a user self-deletes their ad via /my-ads."""
        client = await self.get_client()
        try:
            await client.table("active_posts").update({
                "is_expired": True,
            }).eq("id", post_id).execute()
            logger.info(f"Soft-deleted (expired) active post #{post_id} from database.")
            return True
        except Exception as e:
            logger.error(f"Error soft-deleting active post #{post_id}: {e}", exc_info=True)
            return False

    # -------------------------------------------------------------
    # Ad Interest / Save System (⭐ button)
    # -------------------------------------------------------------

    async def toggle_ad_interest(
        self,
        post_id: int,
        user_id: int,
        message_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Toggles the 'interested' status for a user on a post.
        Resolves post_id via message_id if post_id is missing or 0.
        Returns dict with keys: 'action' ('added'|'removed'), 'count' (total interests), 'post_id'.
        """
        client = await self.get_client()
        try:
            # If post_id is not valid, attempt lookup via message_id
            if (not post_id or post_id <= 0) and message_id:
                lookup = await client.table("active_posts").select("id").eq("message_id", message_id).limit(1).execute()
                if lookup.data:
                    post_id = lookup.data[0]["id"]

            if not post_id or post_id <= 0:
                logger.warning(f"toggle_ad_interest: could not resolve post_id (post_id={post_id}, message_id={message_id})")
                return {"action": "error", "count": 0}

            # Check if already interested
            existing = (
                await client.table("ad_interests")
                .select("id")
                .eq("post_id", post_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )

            if existing.data:
                # Remove interest
                await client.table("ad_interests").delete().eq("post_id", post_id).eq("user_id", user_id).execute()
                action = "removed"
            else:
                # Add interest
                await client.table("ad_interests").insert({
                    "post_id": post_id,
                    "user_id": user_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
                action = "added"

            # Count total interests for post
            count_res = (
                await client.table("ad_interests")
                .select("id", count="exact")
                .eq("post_id", post_id)
                .execute()
            )
            total = count_res.count or 0
            logger.info(f"Ad interest {action} for post #{post_id} by user {user_id}. Total: {total}")
            return {"action": action, "count": total, "post_id": post_id}
        except Exception as e:
            logger.error(f"Error toggling ad interest for post #{post_id}: {e}", exc_info=True)
            return {"action": "error", "count": 0}

    async def get_ad_interest_count(self, post_id: int, message_id: int | None = None) -> int:
        """Returns the total number of users interested in a specific ad post."""
        client = await self.get_client()
        try:
            if (not post_id or post_id <= 0) and message_id:
                lookup = await client.table("active_posts").select("id").eq("message_id", message_id).limit(1).execute()
                if lookup.data:
                    post_id = lookup.data[0]["id"]
            if not post_id or post_id <= 0:
                return 0

            res = (
                await client.table("ad_interests")
                .select("id", count="exact")
                .eq("post_id", post_id)
                .execute()
            )
            return res.count or 0
        except Exception as e:
            logger.error(f"Error getting interest count for post #{post_id}: {e}", exc_info=True)
            return 0

    # -------------------------------------------------------------
    # Templates Operations (NEW)
    # -------------------------------------------------------------

    async def save_template(
        self,
        user_id: int,
        guild_id: int,
        name: str,
        category: str,
        title: str | None,
        content: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Saves or updates an ad template for a user."""
        client = await self.get_client()
        payload = {
            "user_id": user_id,
            "guild_id": guild_id,
            "name": name[:50],
            "category": category,
            "title": title,
            "content": content,
            "tags": tags or [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            response = await client.table("ad_templates").upsert(payload).execute()
            logger.info(f"Saved template '{name}' for user {user_id}")
            return response.data[0] if response.data else payload
        except Exception as e:
            logger.error(f"Error saving template for user {user_id}: {e}", exc_info=True)
            raise

    async def get_user_templates(self, user_id: int, guild_id: int) -> list[dict[str, Any]]:
        """Retrieves all templates for a specific user in a guild."""
        client = await self.get_client()
        try:
            response = (
                await client.table("ad_templates")
                .select("*")
                .eq("user_id", user_id)
                .eq("guild_id", guild_id)
                .order("updated_at", desc=True)
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching templates for user {user_id}: {e}", exc_info=True)
            return []

    async def get_template(self, template_id: int, user_id: int) -> dict[str, Any] | None:
        """Retrieves a specific template by ID (with ownership check)."""
        client = await self.get_client()
        try:
            response = (
                await client.table("ad_templates")
                .select("*")
                .eq("id", template_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"Error fetching template {template_id}: {e}", exc_info=True)
            return None

    async def delete_template(self, template_id: int, user_id: int) -> bool:
        """Deletes a template (with ownership check)."""
        client = await self.get_client()
        try:
            response = (
                await client.table("ad_templates")
                .delete()
                .eq("id", template_id)
                .eq("user_id", user_id)
                .execute()
            )
            return bool(response.data)
        except Exception as e:
            logger.error(f"Error deleting template {template_id}: {e}", exc_info=True)
            return False

    # -------------------------------------------------------------
    # Change Request Operations (NEW)
    # -------------------------------------------------------------

    async def request_changes(
        self,
        submission_id: int,
        reviewer_id: int,
        note: str,
    ) -> dict[str, Any] | None:
        """
        Records a change request from a reviewer.
        Increments change_requests counter and stores the note.
        Returns updated submission or None if max attempts reached.
        """
        client = await self.get_client()
        try:
            # Get current submission
            submission = await self.get_submission(submission_id)
            if not submission:
                return None

            current_requests = submission.get("change_requests", 0)
            max_requests = submission.get("max_change_requests", 3)

            if current_requests >= max_requests:
                logger.warning(f"Submission {submission_id} has reached max change requests ({max_requests})")
                return None

            # Increment counter and save note
            new_count = current_requests + 1
            update_payload = {
                "change_requests": new_count,
                "last_change_note": note[:1000],
                "change_requested_by": reviewer_id,
                "change_requested_at": datetime.now(timezone.utc).isoformat(),
            }

            response = await client.table("submissions").update(update_payload).eq("id", submission_id).execute()

            # Save to history table
            await client.table("submission_change_history").insert({
                "submission_id": submission_id,
                "request_number": new_count,
                "requested_by": reviewer_id,
                "note": note[:1000],
                "old_content": submission.get("content", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            logger.info(f"Change request #{new_count} recorded for submission {submission_id}")
            return response.data[0] if response.data else None

        except Exception as e:
            logger.error(f"Error requesting changes for submission {submission_id}: {e}", exc_info=True)
            raise

    async def update_submission_content(
        self,
        submission_id: int,
        title: str | None,
        content: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Updates the content of a pending submission (for change requests)."""
        client = await self.get_client()
        try:
            # Get current submission for history
            submission = await self.get_submission(submission_id)
            if not submission or submission.get("status") != "PENDING":
                return None

            update_payload: dict[str, Any] = {
                "title": title,
                "content": content,
            }
            if tags is not None:
                update_payload["tags"] = tags

            response = await client.table("submissions").update(update_payload).eq("id", submission_id).execute()

            # Update history if there was a change request
            if submission.get("change_requests", 0) > 0:
                await client.table("submission_change_history").update({
                    "new_content": content,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("submission_id", submission_id).eq("request_number", submission["change_requests"]).execute()

            logger.info(f"Updated content for submission {submission_id}")
            return response.data[0] if response.data else None

        except Exception as e:
            logger.error(f"Error updating submission {submission_id}: {e}", exc_info=True)
            raise

    # -------------------------------------------------------------
    # Review Rating Operations (NEW)
    # -------------------------------------------------------------

    async def save_review_rating(
        self,
        submission_id: int,
        guild_id: int,
        user_id: int,
        reviewer_id: int,
        rating: int,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """Saves a rating from a submitter about their review experience."""
        client = await self.get_client()
        if not (1 <= rating <= 5):
            raise ValueError("Rating must be between 1 and 5")

        payload = {
            "submission_id": submission_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "reviewer_id": reviewer_id,
            "rating": rating,
            "feedback": feedback[:500] if feedback else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            response = await client.table("review_ratings").upsert(payload).execute()
            logger.info(f"Saved rating {rating}/5 for submission {submission_id}")
            return response.data[0] if response.data else payload
        except Exception as e:
            logger.error(f"Error saving rating for submission {submission_id}: {e}", exc_info=True)
            raise

    async def get_reviewer_stats(self, reviewer_id: int, guild_id: int) -> dict[str, Any]:
        """Gets average rating and review count for a reviewer."""
        client = await self.get_client()
        try:
            response = (
                await client.table("review_ratings")
                .select("rating")
                .eq("reviewer_id", reviewer_id)
                .eq("guild_id", guild_id)
                .execute()
            )
            ratings = response.data or []
            if not ratings:
                return {"avg_rating": 0.0, "review_count": 0}

            avg = sum(r["rating"] for r in ratings) / len(ratings)
            return {"avg_rating": round(avg, 2), "review_count": len(ratings)}

        except Exception as e:
            logger.error(f"Error fetching reviewer stats for {reviewer_id}: {e}", exc_info=True)
            return {"avg_rating": 0.0, "review_count": 0}

    # -------------------------------------------------------------
    # Expiry Alerts (NEW)
    # -------------------------------------------------------------

    async def get_posts_needing_expiry_alert(self, hours_before: int = 24) -> list[dict[str, Any]]:
        """
        Returns posts that will expire within X hours and haven't been alerted yet.
        """
        client = await self.get_client()
        try:
            now = datetime.now(timezone.utc)
            alert_threshold = now + timedelta(hours=hours_before)

            response = (
                await client.table("active_posts")
                .select("*, submissions(id, user_id, user_name, title)")
                .eq("is_expired", False)
                .eq("expiry_alert_sent", False)
                .not_.is_("expires_at", "null")
                .lte("expires_at", alert_threshold.isoformat())
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching posts needing expiry alert: {e}", exc_info=True)
            return []

    async def mark_expiry_alert_sent(self, post_id: int) -> bool:
        """Marks that an expiry alert has been sent for a post."""
        client = await self.get_client()
        try:
            await client.table("active_posts").update({"expiry_alert_sent": True}).eq("id", post_id).execute()
            logger.info(f"Marked expiry alert sent for post {post_id}")
            return True
        except Exception as e:
            logger.error(f"Error marking expiry alert for post {post_id}: {e}", exc_info=True)
            return False

    # -------------------------------------------------------------
    # Pinned Ads Operations (NEW)
    # -------------------------------------------------------------

    async def pin_ad(
        self,
        post_id: int,
        staff_id: int,
        duration_hours: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Pins an ad to the top. If duration_hours is provided, sets expiration.
        """
        client = await self.get_client()
        try:
            now = datetime.now(timezone.utc)
            payload: dict[str, Any] = {
                "is_pinned": True,
                "pinned_at": now.isoformat(),
                "pinned_by": staff_id,
            }
            if duration_hours:
                payload["pin_expires_at"] = (now + timedelta(hours=duration_hours)).isoformat()

            response = await client.table("active_posts").update(payload).eq("id", post_id).execute()
            logger.info(f"Pinned ad {post_id} by staff {staff_id}")
            return response.data[0] if response.data else None

        except Exception as e:
            logger.error(f"Error pinning ad {post_id}: {e}", exc_info=True)
            raise

    async def unpin_ad(self, post_id: int) -> bool:
        """Unpins an ad."""
        client = await self.get_client()
        try:
            await client.table("active_posts").update({
                "is_pinned": False,
                "pin_expires_at": None,
            }).eq("id", post_id).execute()
            logger.info(f"Unpinned ad {post_id}")
            return True
        except Exception as e:
            logger.error(f"Error unpinning ad {post_id}: {e}", exc_info=True)
            return False

    async def get_expired_pins(self) -> list[dict[str, Any]]:
        """Returns pinned ads whose pin duration has expired."""
        client = await self.get_client()
        try:
            now = datetime.now(timezone.utc).isoformat()
            response = (
                await client.table("active_posts")
                .select("*")
                .eq("is_pinned", True)
                .not_.is_("pin_expires_at", "null")
                .lte("pin_expires_at", now)
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching expired pins: {e}", exc_info=True)
            return []
