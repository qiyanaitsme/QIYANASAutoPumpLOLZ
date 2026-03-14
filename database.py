"""Database layer with connection pooling and type safety."""

import aiosqlite
import logging
from typing import Self
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Thread:
    """Thread model with immutable fields."""
    id: str
    title: str
    last_bumped: str | None = None


class Database:
    """SQLite database manager with connection pooling and proper error handling."""
    
    __slots__ = ("_db_path", "_connection")
    
    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValueError("db_path is required")
        
        self._db_path = db_path
        self._connection: aiosqlite.Connection | None = None
    
    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()
    
    async def connect(self) -> None:
        """Establish database connection and initialize schema."""
        if self._connection is None:
            try:
                self._connection = await aiosqlite.connect(self._db_path)
                # Enable WAL mode for better concurrency
                await self._connection.execute("PRAGMA journal_mode=WAL")
                await self._initialize_schema()
                logger.info(f"Database connected: {self._db_path}")
            except Exception as e:
                logger.error(f"Failed to connect to database: {e}")
                raise
    
    async def close(self) -> None:
        """Close database connection safely."""
        if self._connection:
            try:
                await self._connection.close()
                self._connection = None
                logger.info("Database connection closed")
            except Exception as e:
                logger.error(f"Error closing database connection: {e}")
    
    async def _initialize_schema(self) -> None:
        """Initialize database schema with tables and indexes."""
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
            
            # Create index for efficient queries on last_bumped
            await self._connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_last_bumped 
                ON threads(last_bumped)
            """)
            
            await self._connection.commit()
            logger.info("Database schema initialized")
        except Exception as e:
            logger.error(f"Failed to initialize schema: {e}")
            raise
    
    def _ensure_connected(self) -> None:
        """Ensure database is connected before operations."""
        if not self._connection:
            raise RuntimeError("Database not connected. Call connect() first.")
    
    async def add_thread(self, thread_id: str, title: str) -> bool:
        """
        Add thread to database.
        
        Args:
            thread_id: Unique thread identifier
            title: Thread title
            
        Returns:
            True if added successfully, False if thread already exists
        """
        self._ensure_connected()
        
        try:
            await self._connection.execute(
                "INSERT INTO threads (id, title) VALUES (?, ?)",
                (thread_id, title)
            )
            await self._connection.commit()
            return True
        except aiosqlite.IntegrityError:
            # Thread already exists (PRIMARY KEY constraint)
            return False
        except Exception as e:
            logger.error(f"Error adding thread {thread_id}: {e}")
            raise
    
    async def get_all_threads(self) -> list[Thread]:
        """
        Get all threads ordered by ID.
        
        Returns:
            List of Thread objects
        """
        self._ensure_connected()
        
        try:
            async with self._connection.execute(
                "SELECT id, title, last_bumped FROM threads ORDER BY id"
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    Thread(id=row[0], title=row[1], last_bumped=row[2])
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error fetching all threads: {e}")
            raise
    
    async def get_threads_to_bump(self, interval_hours: float) -> list[Thread]:
        """
        Get threads ready for bumping based on interval.
        
        Args:
            interval_hours: Minimum hours since last bump
            
        Returns:
            List of Thread objects ready to bump
        """
        self._ensure_connected()
        
        if interval_hours < 0:
            raise ValueError("interval_hours must be non-negative")
        
        try:
            async with self._connection.execute(
                """
                SELECT id, title, last_bumped FROM threads
                WHERE last_bumped IS NULL 
                   OR datetime(last_bumped, '+' || ? || ' hours') <= datetime('now')
                ORDER BY last_bumped ASC NULLS FIRST
                """,
                (interval_hours,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    Thread(id=row[0], title=row[1], last_bumped=row[2])
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error fetching threads to bump: {e}")
            raise
    
    async def update_last_bumped(self, thread_id: str) -> None:
        """
        Update last bumped timestamp for thread to current time.
        
        Args:
            thread_id: Thread identifier to update
        """
        self._ensure_connected()
        
        try:
            await self._connection.execute(
                "UPDATE threads SET last_bumped = datetime('now') WHERE id = ?",
                (thread_id,)
            )
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Error updating last_bumped for thread {thread_id}: {e}")
            raise
    
    async def delete_thread(self, thread_id: str) -> bool:
        """
        Delete thread from database.
        
        Args:
            thread_id: Thread identifier to delete
            
        Returns:
            True if deleted successfully, False if thread not found
        """
        self._ensure_connected()
        
        try:
            cursor = await self._connection.execute(
                "DELETE FROM threads WHERE id = ?",
                (thread_id,)
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting thread {thread_id}: {e}")
            raise
    
    async def get_thread_count(self) -> int:
        """
        Get total number of threads in database.
        
        Returns:
            Total thread count
        """
        self._ensure_connected()
        
        try:
            async with self._connection.execute("SELECT COUNT(*) FROM threads") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"Error getting thread count: {e}")
            raise
    
    async def thread_exists(self, thread_id: str) -> bool:
        """
        Check if thread exists in database.
        
        Args:
            thread_id: Thread identifier to check
            
        Returns:
            True if thread exists, False otherwise
        """
        self._ensure_connected()
        
        try:
            async with self._connection.execute(
                "SELECT 1 FROM threads WHERE id = ? LIMIT 1",
                (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
        except Exception as e:
            logger.error(f"Error checking thread existence {thread_id}: {e}")
            raise
