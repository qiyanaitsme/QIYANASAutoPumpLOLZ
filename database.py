import aiosqlite
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class Thread:
    id: str
    title: str
    last_bumped: Optional[str] = None


class Database:
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    last_bumped TEXT
                )
            """)
            await db.commit()
    
    async def add_thread(self, thread_id: str, title: str) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO threads (id, title) VALUES (?, ?)",
                    (thread_id, title)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"Error adding thread {thread_id}: {e}")
            return False
    
    async def get_all_threads(self) -> List[Thread]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, title, last_bumped FROM threads ORDER BY id"
            ) as cursor:
                rows = await cursor.fetchall()
                return [Thread(id=r[0], title=r[1], last_bumped=r[2]) for r in rows]
    
    async def get_threads_to_bump(self, interval_hours: float) -> List[Thread]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(f"""
                SELECT id, title, last_bumped FROM threads
                WHERE last_bumped IS NULL 
                OR datetime(last_bumped, '+{interval_hours} hours') <= datetime('now')
                ORDER BY last_bumped ASC NULLS FIRST
            """) as cursor:
                rows = await cursor.fetchall()
                return [Thread(id=r[0], title=r[1], last_bumped=r[2]) for r in rows]
    
    async def update_last_bumped(self, thread_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE threads SET last_bumped = datetime('now') WHERE id = ?",
                (thread_id,)
            )
            await db.commit()
    
    async def delete_thread(self, thread_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM threads WHERE id = ?",
                (thread_id,)
            )
            await db.commit()
            return cursor.rowcount > 0
