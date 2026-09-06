"""Database layer with dynamic settings and stats persistence."""

import aiosqlite
import logging
from typing import Self
from dataclasses import dataclass

from config_manager import Config


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Thread:
    id: str
    title: str
    last_bumped: str | None = None


@dataclass(frozen=True, slots=True)
class BumpStats:
    total_bumps: int
    successful_bumps: int
    last_bump_time: str | None = None


class Database:
    __slots__ = ("_db_path", "_connection")

    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValueError("db_path is required")

        self._db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._connection is None:
            try:
                self._connection = await aiosqlite.connect(self._db_path)
                await self._connection.execute("PRAGMA journal_mode=WAL")
                await self._connection.execute("PRAGMA foreign_keys=ON")
                await self._initialize_schema()
                logger.info(f"Database connected: {self._db_path}")
            except Exception as e:
                logger.error(f"Failed to connect to database: {e}")
                raise

    async def close(self) -> None:
        if self._connection:
            try:
                await self._connection.close()
                self._connection = None
                logger.info("Database connection closed")
            except Exception as e:
                logger.error(f"Error closing database connection: {e}")

    async def _initialize_schema(self) -> None:
        if not self._connection:
            raise RuntimeError("Database not connected")

        try:
            await self._connection.execute("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    last_bumped TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await self._connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_last_bumped
                ON threads(last_bumped)
            """)

            await self._connection.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await self._connection.execute("""
                CREATE TABLE IF NOT EXISTS bump_stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_bumps INTEGER DEFAULT 0,
                    successful_bumps INTEGER DEFAULT 0,
                    last_bump_time TEXT
                )
            """)

            await self._connection.commit()
            logger.info("Database schema initialized")
        except Exception as e:
            logger.error(f"Failed to initialize schema: {e}")
            raise

    async def seed_defaults(self, config: Config) -> None:
        await self._seed_settings(config)
        await self._seed_stats()

    async def _seed_settings(self, config: Config) -> None:
        defaults = {
            "bump_interval_minutes": str(config.scheduling.bump_interval_minutes),
            "enable_auto_bump": str(config.scheduling.enable_auto_bump).lower(),
            "batch_size": str(config.api.batch_size),
        }
        for key, value in defaults.items():
            await self._connection.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await self._connection.commit()

    async def _seed_stats(self) -> None:
        await self._connection.execute(
            "INSERT OR IGNORE INTO bump_stats (id, total_bumps, successful_bumps) VALUES (1, 0, 0)"
        )
        await self._connection.commit()

    def _ensure_connected(self) -> None:
        if not self._connection:
            raise RuntimeError("Database not connected. Call connect() first.")

    # ─── Settings ───────────────────────────────────────────────

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        self._ensure_connected()
        async with self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        self._ensure_connected()
        await self._connection.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, value),
        )
        await self._connection.commit()

    async def get_all_settings(self) -> dict[str, str]:
        self._ensure_connected()
        async with self._connection.execute("SELECT key, value FROM settings") as cursor:
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

    # ─── Bump Statistics ────────────────────────────────────────

    async def get_bump_stats(self) -> BumpStats:
        self._ensure_connected()
        async with self._connection.execute(
            "SELECT total_bumps, successful_bumps, last_bump_time FROM bump_stats WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return BumpStats(
                    total_bumps=row[0],
                    successful_bumps=row[1],
                    last_bump_time=row[2],
                )
            return BumpStats(total_bumps=0, successful_bumps=0)

    async def increment_bump_stats(self, success_count: int, total_count: int) -> None:
        self._ensure_connected()
        await self._connection.execute(
            """
            UPDATE bump_stats SET
                total_bumps = total_bumps + ?,
                successful_bumps = successful_bumps + ?,
                last_bump_time = datetime('now')
            WHERE id = 1
            """,
            (total_count, success_count),
        )
        await self._connection.commit()

    # ─── Threads ────────────────────────────────────────────────

    async def add_thread(self, thread_id: str, title: str) -> bool:
        self._ensure_connected()
        try:
            await self._connection.execute(
                "INSERT INTO threads (id, title) VALUES (?, ?)",
                (thread_id, title),
            )
            await self._connection.commit()
            return True
        except aiosqlite.IntegrityError:
            return False
        except Exception as e:
            logger.error(f"Error adding thread {thread_id}: {e}")
            raise

    async def update_thread_title(self, thread_id: str, title: str) -> bool:
        self._ensure_connected()
        try:
            cursor = await self._connection.execute(
                "UPDATE threads SET title = ? WHERE id = ?", (title, thread_id)
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating title for thread {thread_id}: {e}")
            raise

    async def get_all_threads(self) -> list[Thread]:
        self._ensure_connected()
        try:
            async with self._connection.execute(
                "SELECT id, title, last_bumped FROM threads ORDER BY id"
            ) as cursor:
                rows = await cursor.fetchall()
                return [Thread(id=row[0], title=row[1], last_bumped=row[2]) for row in rows]
        except Exception as e:
            logger.error(f"Error fetching all threads: {e}")
            raise

    async def get_threads_to_bump(self, interval_minutes: float) -> list[Thread]:
        """Threads due for a bump: the interval has passed since the last one."""
        self._ensure_connected()
        if interval_minutes < 0:
            raise ValueError("interval_minutes must be non-negative")
        try:
            async with self._connection.execute(
                """
                SELECT id, title, last_bumped FROM threads
                WHERE last_bumped IS NULL
                   OR datetime(last_bumped, '+' || ? || ' minutes') <= datetime('now')
                ORDER BY last_bumped ASC NULLS FIRST
                """,
                (interval_minutes,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [Thread(id=row[0], title=row[1], last_bumped=row[2]) for row in rows]
        except Exception as e:
            logger.error(f"Error fetching threads to bump: {e}")
            raise

    async def update_last_bumped(self, thread_id: str) -> None:
        self._ensure_connected()
        try:
            await self._connection.execute(
                "UPDATE threads SET last_bumped = datetime('now') WHERE id = ?",
                (thread_id,),
            )
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Error updating last_bumped for thread {thread_id}: {e}")
            raise

    async def delete_thread(self, thread_id: str) -> bool:
        self._ensure_connected()
        try:
            cursor = await self._connection.execute(
                "DELETE FROM threads WHERE id = ?", (thread_id,)
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting thread {thread_id}: {e}")
            raise

    async def get_thread_count(self) -> int:
        self._ensure_connected()
        try:
            async with self._connection.execute("SELECT COUNT(*) FROM threads") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"Error getting thread count: {e}")
            raise

    async def thread_exists(self, thread_id: str) -> bool:
        self._ensure_connected()
        try:
            async with self._connection.execute(
                "SELECT 1 FROM threads WHERE id = ? LIMIT 1", (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
        except Exception as e:
            logger.error(f"Error checking thread existence {thread_id}: {e}")
            raise
