"""Lolz API client with batch request support and proper error handling."""

import re
import logging
import aiohttp
import asyncio
from typing import Self, Sequence
from dataclasses import dataclass
from enum import Enum


# Constants
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2

logger = logging.getLogger(__name__)


class BumpStatus(Enum):
    """Bump operation status."""
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BumpResult:
    """Result of bump operation."""
    success: bool
    message: str
    thread_id: str
    status: BumpStatus


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """Thread information from API."""
    thread_id: str
    title: str


class APIClient:
    """Lolz API client with batch request support and connection pooling."""
    
    __slots__ = ("_base_url", "_auth_token", "_session", "_batch_size")
    
    def __init__(self, base_url: str, auth_token: str, batch_size: int = 10) -> None:
        if not base_url or not auth_token:
            raise ValueError("base_url and auth_token are required")
        
        if batch_size < 1 or batch_size > 10:
            raise ValueError("batch_size must be between 1 and 10")
        
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._session: aiohttp.ClientSession | None = None
        self._batch_size = batch_size
    
    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()
    
    async def start(self) -> None:
        """Initialize HTTP session with connection pooling."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._auth_token}",
                "User-Agent": "AutoBumpBot/4.0",
                "Content-Type": "application/json"
            }
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
                connector=connector
            )
    
    async def close(self) -> None:
        """Close HTTP session and cleanup resources."""
        if self._session and not self._session.closed:
            await self._session.close()
            # Wait for connections to close properly
            await asyncio.sleep(0.25)
            self._session = None
    
    @staticmethod
    def _extract_error_message(error_msg: str) -> str:
        """Extract and clean error message from API response."""
        if not error_msg:
            return "Unknown error"
        
        # Remove HTML tags
        error_msg = re.sub(r"<br\s*/?>", "\n", error_msg)
        error_msg = re.sub(r"<[^>]+>", "", error_msg)
        
        # Split by newlines and filter empty parts
        parts = [p.strip() for p in error_msg.split("\n") if p.strip()]
        
        if not parts:
            return "Unknown error"
        
        # Look for rate limit message first
        for part in parts:
            if "должны подождать" in part.lower() or "должен подождать" in part.lower():
                return part
        
        # Return last meaningful part
        return parts[-1]
    
    async def get_thread_info(self, thread_id: str) -> ThreadInfo | None:
        """Get single thread information from API (legacy method, prefer get_threads_info_batch)."""
        results = await self.get_threads_info_batch([thread_id])
        return results[0] if results else None
    
    async def get_threads_info_batch(self, thread_ids: Sequence[str]) -> list[ThreadInfo | None]:
        """Get multiple thread information using batch API (up to 10 per request)."""
        if not self._session:
            await self.start()
        
        if not thread_ids:
            return []
        
        # Process in batches of up to batch_size
        all_results: list[ThreadInfo | None] = []
        
        for i in range(0, len(thread_ids), self._batch_size):
            batch = thread_ids[i:i + self._batch_size]
            batch_results = await self._fetch_threads_info_batch(batch)
            all_results.extend(batch_results)
        
        return all_results
    
    async def _fetch_threads_info_batch(self, thread_ids: Sequence[str]) -> list[ThreadInfo | None]:
        """Execute single batch request to get info for up to 10 threads with retry logic."""
        if not thread_ids:
            return []
        
        # Build batch request payload - must use full URLs for batch API
        batch_payload = [
            {
                "method": "GET",
                "uri": f"{self._base_url}/threads/{thread_id}"
            }
            for thread_id in thread_ids
        ]
        
        batch_url = f"{self._base_url}/batch"
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                async with self._session.post(batch_url, json=batch_payload) as resp:
                    logger.info(f"Batch API request for {len(thread_ids)} threads, status: {resp.status}")
                    
                    if resp.status == 401:
                        raise ValueError("Invalid API token - check your configuration")
                    
                    if resp.status != 200:
                        response_text = await resp.text()
                        logger.error(f"Batch API failed with status {resp.status}: {response_text}")
                        if attempt < MAX_RETRY_ATTEMPTS - 1:
                            await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                            continue
                        # Final attempt failed
                        return [None] * len(thread_ids)
                    
                    # Parse batch response
                    batch_response = await resp.json()
                    logger.debug(f"Batch response: {batch_response}")
                    
                    # Validate response structure - API returns {"jobs": {...}, "system_info": {...}}
                    if not isinstance(batch_response, dict) or "jobs" not in batch_response:
                        logger.error(f"Batch response missing 'jobs' key: {batch_response}")
                        return [None] * len(thread_ids)
                    
                    jobs = batch_response["jobs"]
                    if not isinstance(jobs, dict):
                        logger.error(f"Jobs is not a dict: {type(jobs)}")
                        return [None] * len(thread_ids)
                    
                    # Process each thread
                    results: list[ThreadInfo | None] = []
                    for thread_id in thread_ids:
                        uri = f"{self._base_url}/threads/{thread_id}"
                        
                        if uri not in jobs:
                            logger.warning(f"Thread {thread_id} not in jobs response")
                            results.append(None)
                            continue
                        
                        job_data = jobs[uri]
                        if not isinstance(job_data, dict):
                            logger.warning(f"Job data for {thread_id} is not a dict")
                            results.append(None)
                            continue
                        
                        # Extract thread info
                        thread_data = job_data.get("thread", {})
                        if not isinstance(thread_data, dict):
                            logger.warning(f"Thread data for {thread_id} is not a dict")
                            results.append(None)
                            continue
                        
                        thread_info = ThreadInfo(
                            thread_id=thread_id,
                            title=thread_data.get("thread_title", "Unknown")
                        )
                        results.append(thread_info)
                    
                    return results
                    
            except aiohttp.ClientError as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                raise ConnectionError(f"Network error after {MAX_RETRY_ATTEMPTS} attempts: {e}") from e
            except ValueError:
                raise
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                raise RuntimeError(f"Unexpected error getting thread info: {e}") from e
        
        return [None] * len(thread_ids)
    
    async def bump_thread(self, thread_id: str) -> BumpResult:
        """Bump single thread via API (legacy method, prefer bump_threads_batch)."""
        results = await self.bump_threads_batch([thread_id])
        return results[0]
    
    async def bump_threads_batch(self, thread_ids: Sequence[str]) -> list[BumpResult]:
        """Bump multiple threads using batch API (up to 10 per request)."""
        if not self._session:
            await self.start()
        
        if not thread_ids:
            return []
        
        # Process in batches of up to batch_size
        all_results: list[BumpResult] = []
        
        for i in range(0, len(thread_ids), self._batch_size):
            batch = thread_ids[i:i + self._batch_size]
            batch_results = await self._execute_bump_batch(batch)
            all_results.extend(batch_results)
        
        return all_results
    
    async def _execute_bump_batch(self, thread_ids: Sequence[str]) -> list[BumpResult]:
        """Execute single batch bump request for up to 10 threads with retry logic."""
        if not thread_ids:
            return []
        
        # Build batch request payload - must use full URLs for batch API
        batch_payload = [
            {
                "method": "POST",
                "uri": f"{self._base_url}/threads/{thread_id}/bump"
            }
            for thread_id in thread_ids
        ]
        
        batch_url = f"{self._base_url}/batch"
        
        logger.info(f"🚀 Executing bump batch request for {len(thread_ids)} threads: {thread_ids}")
        logger.debug(f"Bump batch payload: {batch_payload}")
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                async with self._session.post(batch_url, json=batch_payload) as resp:
                    logger.info(f"Bump batch API response: HTTP {resp.status}")
                    
                    if resp.status == 401:
                        # Unauthorized - return error for all threads
                        return [
                            BumpResult(
                                success=False,
                                message=f"Тема {tid}: Неверный токен API",
                                thread_id=tid,
                                status=BumpStatus.UNAUTHORIZED
                            )
                            for tid in thread_ids
                        ]
                    
                    if resp.status != 200:
                        if attempt < MAX_RETRY_ATTEMPTS - 1:
                            await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                            continue
                        # Final attempt failed
                        return [
                            BumpResult(
                                success=False,
                                message=f"Тема {tid}: Ошибка batch запроса (HTTP {resp.status})",
                                thread_id=tid,
                                status=BumpStatus.ERROR
                            )
                            for tid in thread_ids
                        ]
                    
                    # Parse batch response
                    batch_response = await resp.json()
                    logger.debug(f"Bump batch response: {batch_response}")
                    
                    # Validate response structure - API returns {"jobs": {...}, "system_info": {...}}
                    if not isinstance(batch_response, dict) or "jobs" not in batch_response:
                        logger.error(f"Bump batch response missing 'jobs' key")
                        return [
                            BumpResult(
                                success=False,
                                message=f"Тема {tid}: Неверный формат ответа API",
                                thread_id=tid,
                                status=BumpStatus.ERROR
                            )
                            for tid in thread_ids
                        ]
                    
                    jobs = batch_response["jobs"]
                    if not isinstance(jobs, dict):
                        logger.error(f"Bump jobs is not a dict")
                        return [
                            BumpResult(
                                success=False,
                                message=f"Тема {tid}: Неверный формат ответа API",
                                thread_id=tid,
                                status=BumpStatus.ERROR
                            )
                            for tid in thread_ids
                        ]
                    
                    # Process each thread
                    results: list[BumpResult] = []
                    for thread_id in thread_ids:
                        uri = f"{self._base_url}/threads/{thread_id}/bump"
                        
                        if uri not in jobs:
                            logger.error(f"Bump for thread {thread_id} not in jobs response | URI: {uri}")
                            results.append(BumpResult(
                                success=False,
                                message=f"Тема {thread_id}: Нет ответа от сервера",
                                thread_id=thread_id,
                                status=BumpStatus.ERROR
                            ))
                            continue
                        
                        job_data = jobs[uri]
                        logger.info(f"Thread {thread_id} raw bump response: {job_data}")
                        
                        # Empty list [] or empty dict {} means success
                        if isinstance(job_data, (list, dict)) and len(job_data) == 0:
                            logger.info(f"Thread {thread_id} bumped successfully (empty response)")
                            results.append(BumpResult(
                                success=True,
                                message=f"✅ Тема {thread_id} поднята успешно",
                                thread_id=thread_id,
                                status=BumpStatus.SUCCESS
                            ))
                        elif isinstance(job_data, dict) and "errors" in job_data:
                            error_msg = self._extract_error_message(str(job_data["errors"]))
                            logger.error(
                                f"Thread {thread_id} bump failed | "
                                f"Errors: {job_data['errors']} | "
                                f"Extracted: {error_msg}"
                            )
                            results.append(BumpResult(
                                success=False,
                                message=f"Тема {thread_id}: {error_msg}",
                                thread_id=thread_id,
                                status=BumpStatus.ERROR
                            ))
                        elif isinstance(job_data, dict) and "_job_result" in job_data:
                            job_result = str(job_data.get("_job_result", ""))
                            job_message = str(job_data.get("_job_message", ""))
                            if job_result == "error":
                                error_text = job_message
                                if not error_text.strip():
                                    errors = job_data.get("errors")
                                    if errors:
                                        if isinstance(errors, list):
                                            error_text = str(errors[0]) if errors else ""
                                        elif isinstance(errors, str):
                                            error_text = errors
                                        else:
                                            error_text = str(errors)
                                if not error_text.strip():
                                    error_text = str(job_data.get("error", ""))
                                error_msg = self._extract_error_message(error_text) or "Ошибка API (см. логи)"
                                logger.error(
                                    f"Thread {thread_id} bump failed | "
                                    f"Job error: {error_msg}"
                                )
                                results.append(BumpResult(
                                    success=False,
                                    message=f"Тема {thread_id}: {error_msg}",
                                    thread_id=thread_id,
                                    status=BumpStatus.ERROR
                                ))
                            else:
                                logger.info(f"Thread {thread_id} bumped successfully (job_result={job_result}, job_message={job_message})")
                                results.append(BumpResult(
                                    success=True,
                                    message=f"✅ Тема {thread_id} поднята успешно",
                                    thread_id=thread_id,
                                    status=BumpStatus.SUCCESS
                                ))
                        else:
                            logger.warning(f"Thread {thread_id} unknown bump response (type={type(job_data).__name__}): {job_data}")
                            results.append(BumpResult(
                                success=False,
                                message=f"Тема {thread_id}: Неизвестный ответ ({type(job_data).__name__})",
                                thread_id=thread_id,
                                status=BumpStatus.ERROR
                            ))
                    
                    logger.info(f"Bump batch processed: {len(results)} results")
                    return results
                    
            except aiohttp.ClientError as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                # Network error - return error for all threads
                return [
                    BumpResult(
                        success=False,
                        message=f"Тема {tid}: Ошибка сети - {str(e)}",
                        thread_id=tid,
                        status=BumpStatus.ERROR
                    )
                    for tid in thread_ids
                ]
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                # Unexpected error - return error for all threads
                return [
                    BumpResult(
                        success=False,
                        message=f"Тема {tid}: Неожиданная ошибка - {str(e)}",
                        thread_id=tid,
                        status=BumpStatus.ERROR
                    )
                    for tid in thread_ids
                ]
        
        # Should never reach here, but just in case
        return [
            BumpResult(
                success=False,
                message=f"Тема {tid}: Превышено количество попыток",
                thread_id=tid,
                status=BumpStatus.ERROR
            )
            for tid in thread_ids
        ]
    
    def _parse_bump_response(self, thread_id: str, response_data: dict) -> BumpResult:
        """Parse individual bump response from batch result."""
        # Check for errors in response
        if "errors" in response_data and response_data["errors"]:
            errors = response_data["errors"]
            if isinstance(errors, list) and errors:
                error_msg = str(errors[0])
                cleaned_msg = self._extract_error_message(error_msg)
                
                # Determine status
                status = BumpStatus.ERROR
                if "подождать" in cleaned_msg.lower():
                    status = BumpStatus.RATE_LIMITED
                
                return BumpResult(
                    success=False,
                    message=f"Тема {thread_id}: {cleaned_msg}",
                    thread_id=thread_id,
                    status=status
                )
        
        # Check HTTP status code in batch response
        status_code = response_data.get("_status_code", 200)
        
        if status_code == 200:
            return BumpResult(
                success=True,
                message=f"✅ Тема {thread_id} поднята успешно",
                thread_id=thread_id,
                status=BumpStatus.SUCCESS
            )
        elif status_code == 404:
            return BumpResult(
                success=False,
                message=f"Тема {thread_id}: Не найдена",
                thread_id=thread_id,
                status=BumpStatus.NOT_FOUND
            )
        elif status_code == 401:
            return BumpResult(
                success=False,
                message=f"Тема {thread_id}: Неверный токен API",
                thread_id=thread_id,
                status=BumpStatus.UNAUTHORIZED
            )
        
        return BumpResult(
            success=False,
            message=f"Тема {thread_id}: HTTP {status_code}",
            thread_id=thread_id,
            status=BumpStatus.ERROR
        )
