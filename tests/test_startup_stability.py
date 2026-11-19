from datetime import datetime, timedelta, timezone
import asyncio
from pathlib import Path

from database.supabase_db import SupabaseDatabase


class FakeResponse:
    def __init__(self, data=None) -> None:
        self.data = data


class FakeQuery:
    def __init__(self, owner, table_name: str) -> None:
        self.owner = owner
        self.table_name = table_name

    def select(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    async def execute(self):
        self.owner.execute_count += 1
        if self.owner.missing_table == self.table_name:
            raise RuntimeError(f"missing table: {self.table_name}")
        if self.table_name == "server_configs":
            return FakeResponse([{"guild_id": 123, "announcement_channel_id": 456}])
        return FakeResponse([{"id": 1}])


class FakeClient:
    def __init__(self, missing_table: str | None = None) -> None:
        self.missing_table = missing_table
        self.execute_count = 0

    def table(self, table_name: str) -> FakeQuery:
        return FakeQuery(self, table_name)


def test_connection_check_requires_all_core_tables() -> None:
    db = SupabaseDatabase("https://example.supabase.co", "test-key")
    db.client = FakeClient(missing_table="ad_interests")

    assert asyncio.run(db.check_connection()) is False


def test_connection_check_passes_when_core_tables_exist() -> None:
    db = SupabaseDatabase("https://example.supabase.co", "test-key")
    db.client = FakeClient()

    assert asyncio.run(db.check_connection()) is True


def test_guild_config_cache_is_used_until_ttl_expires() -> None:
    db = SupabaseDatabase("https://example.supabase.co", "test-key")
    fake_client = FakeClient()
    db.client = fake_client

    first = asyncio.run(db.get_guild_config(123))
    second = asyncio.run(db.get_guild_config(123))

    assert first == second
    assert fake_client.execute_count == 1

    db._guild_configs_fetched_at[123] = datetime.now(timezone.utc) - timedelta(
        seconds=db.GUILD_CONFIG_CACHE_TTL_SECONDS + 1
    )
    asyncio.run(db.get_guild_config(123))

    assert fake_client.execute_count == 2


def test_moderation_experience_migration_defines_audit_and_templates() -> None:
    migration = (
        Path(__file__).parent.parent
        / "database"
        / "moderation_experience_migration.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public.moderation_audit_log" in migration
    assert "CREATE TABLE IF NOT EXISTS public.moderation_reply_templates" in migration
    assert "CHANGE_REQUEST" in migration
