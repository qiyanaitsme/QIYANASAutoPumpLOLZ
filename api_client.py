import aiohttp
import re
from typing import Optional, Tuple
from dataclasses import dataclass


@dataclass
class BumpResult:
    success: bool
    message: str
    thread_id: str


@dataclass
class ThreadInfo:
    thread_id: str
    title: str


class APIClient:
    
    def __init__(self, base_url: str, auth_token: str):
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        self.session: Optional[aiohttp.ClientSession] = None
    
    def _clean_error_message(self, error_msg: str) -> str:
        error_msg = re.sub(r'<br\s*/?>', '\n', error_msg)
        error_msg = re.sub(r'<[^>]+>', '', error_msg)
        
        parts = [p.strip() for p in error_msg.split('\n') if p.strip()]
        
        if len(parts) > 1:
            for part in parts:
                if 'должны подождать' in part.lower() or 'должен подождать' in part.lower():
                    return part
            return parts[-1]
        
        return error_msg.strip()
    
    async def start(self):
        if self.session is None or self.session.closed:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.auth_token}",
                "User-Agent": "AutoBumpBot/3.0",
                "Content-Type": "application/json"
            }
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def get_thread_info(self, thread_id: str) -> Optional[ThreadInfo]:
        if not self.session:
            await self.start()
        
        url = f"{self.base_url}/threads/{thread_id}"
        
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    thread_data = data.get("thread", {})
                    return ThreadInfo(
                        thread_id=thread_id,
                        title=thread_data.get("thread_title", "Unknown")
                    )
        except Exception as e:
            print(f"Error getting thread info {thread_id}: {e}")
        
        return None
    
    async def bump_thread(self, thread_id: str) -> BumpResult:
        if not self.session:
            await self.start()
        
        url = f"{self.base_url}/threads/{thread_id}/bump"
        
        try:
            async with self.session.post(url) as resp:
                data = await resp.json()
                
                if "errors" in data and data["errors"]:
                    error_msg = data["errors"][0]
                    cleaned_msg = self._clean_error_message(error_msg)
                    return BumpResult(
                        success=False,
                        message=f"Тема {thread_id}: {cleaned_msg}",
                        thread_id=thread_id
                    )
                
                if resp.status == 200:
                    return BumpResult(
                        success=True,
                        message=f"✅ Тема {thread_id} поднята успешно",
                        thread_id=thread_id
                    )
                
                return BumpResult(
                    success=False,
                    message=f"Тема {thread_id}: HTTP {resp.status}",
                    thread_id=thread_id
                )
                
        except Exception as e:
            return BumpResult(
                success=False,
                message=f"Тема {thread_id}: Ошибка - {str(e)}",
                thread_id=thread_id
            )
