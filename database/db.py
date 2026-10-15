"""
AdSentry Database Handler
Asynchronous SQLite connection manager built on top of aiosqlite.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import aiosqlite

logger = logging.getLogger("AdSentry.Database")


class Database:
    """Manages asynchronous SQLite connections and queries for AdSentry."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Establishes the database connection and enables foreign key constraints."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA foreign_keys = ON;")
        await self._connection.commit()
        logger.info(f"Connected to database at {self.db_path}")

    async def init_db(self, schema_path: Path | None = None) -> None:
        """Initializes tables and indexes from schema.sql."""
        if self._connection is None:
            await self.connect()

        if schema_path is None:
            schema_path = Path(__file__).parent / "schema.sql"

        if not schema_path.exists():
            raise FileNotFoundError(f"Schema file not found at {schema_path}")

        with open(schema_path, "r", encoding="utf-8") as f:
            schema_script = f.read()

        assert self._connection is not None
        await self._connection.executescript(schema_script)
        await self._connection.commit()
        logger.info("Database schema applied successfully.")

    async def close(self) -> None:
        """Closes the database connection cleanly."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Database connection closed.")

    async def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        """Executes a query and commits the transaction."""
        if self._connection is None:
            await self.connect()
        assert self._connection is not None
        cursor = await self._connection.execute(query, parameters)
        await self._connection.commit()
        return cursor

    async def execute_insert(self, query: str, parameters: tuple[Any, ...] = ()) -> int:
        """Executes an INSERT statement and returns the last inserted row ID."""
        cursor = await self.execute(query, parameters)
        return cursor.lastrowid or 0

    async def fetch_one(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        """Fetches a single row."""
        if self._connection is None:
            await self.connect()
        assert self._connection is not None
        async with self._connection.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def fetch_all(
        self, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        """Fetches all rows."""
        if self._connection is None:
            await self.connect()
        assert self._connection is not None
        async with self._connection.execute(query, parameters) as cursor:
            return await cursor.fetchall()
