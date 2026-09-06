"""Lolz API client (spec-compliant: forum.json / Lolzteam Public API v1.1.44a).

Covers: POST /batch (id-keyed jobs), GET /threads/{id}, POST /threads/{id}/bump,
GET /threads?tab=mythreads, GET /users/me.
Rate limits per spec: GET 300/min, non-GET 30/min, /batch 20/min (429 + X-RateLimit-*).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Self, Sequence

import aiohttp


# Constants
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
MAX_RATE_LIMIT_WAIT_SECONDS = 180
DEFAULT_429_WAIT_SECONDS = RETRY_DELAY_SECONDS * 5
# "status" values that mean success in the documented bump response {status, message}
SUCCESSFUL_STATUSES = {"ok", "success", "true"}

logger = logging.getLogger(__name__)


class BumpStatus(Enum):
    """Bump operation status."""
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"


class LolzAPIError(Exception):
    """Base error for Lolz API failures."""


class InvalidTokenError(LolzAPIError):
    """API token is invalid or missing scopes."""


class BatchRequestError(LolzAPIError):
    """Batch/API request failed in a way that won't be fixed by retrying."""


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


@dataclass(frozen=True, slots=True)
class ThreadsPage:
    """Page of GET /threads listing."""
    threads: list[ThreadInfo]
    total: int


def extract_error_message(error_msg: str) -> str:
    """Extract and clean error message from API response."""
    if not error_msg:
        return ""

    # Remove HTML tags
    error_msg = re.sub(r"<br\s*/?>", "\n", error_msg)
    error_msg = re.sub(r"<[^>]+>", "", error_msg)

    # Split by newlines and filter empty parts
    parts = [p.strip() for p in error_msg.split("\n") if p.strip()]

    if not parts:
        return ""

    # Look for rate limit message first
    for part in parts:
        if "должны подождать" in part.lower() or "должен подождать" in part.lower():
            return part

    # Return last meaningful part
    return parts[-1]


def _errors_to_text(errors: object) -> str:
    """Normalize an API 'errors' payload (list/dict/str/other) into plain text."""
    if isinstance(errors, list):
        return "; ".join(str(e) for e in errors)
    if isinstance(errors, dict):
        return "; ".join(str(v) for v in errors.values())
    return str(errors)


def _error_status(message: str) -> BumpStatus:
    if "подождать" in message.lower():
        return BumpStatus.RATE_LIMITED
    return BumpStatus.ERROR


def parse_bump_job_result(thread_id: str, job_data: object) -> BumpResult:
    """Parse a single job result of a batch bump request.

    Handles every documented/observed shape:
    - ``[]`` / ``{}`` — empty response means success (observed on live API)
    - ``{"_job_result": "error", "_job_message": ...}`` — legacy error wrapper
    - ``{"errors": [...]}`` — error response
    - ``{"status": ..., "message": ..., "system_info": ...}`` — documented
      POST /threads/{id}/bump 200 response shape
    """
    if job_data is None:
        return BumpResult(False, f"Тема {thread_id}: Нет ответа от сервера", thread_id, BumpStatus.ERROR)

    if isinstance(job_data, (list, dict)) and len(job_data) == 0:
        return BumpResult(True, f"✅ Тема {thread_id} поднята успешно", thread_id, BumpStatus.SUCCESS)

    if not isinstance(job_data, dict):
        logger.warning(f"Thread {thread_id} unknown bump response (type={type(job_data).__name__}): {job_data}")
        return BumpResult(
            False,
            f"Тема {thread_id}: Неизвестный ответ ({type(job_data).__name__})",
            thread_id,
            BumpStatus.ERROR,
        )

    # Legacy wrapper observed on live API
    if "_job_result" in job_data:
        job_result = str(job_data.get("_job_result", "")).lower()
        if job_result == "error":
            error_text = str(job_data.get("_job_message", "") or "")
            if not error_text.strip():
                errors = job_data.get("errors")
                if errors:
                    error_text = _errors_to_text(errors)
            if not error_text.strip():
                error_text = str(job_data.get("error", ""))
            error_msg = extract_error_message(error_text) or "Ошибка API без текста (см. raw response в логах)"
            logger.error(
                f"Thread {thread_id} bump failed (legacy wrapper) | "
                f"Raw: {job_data} | Extracted: {error_msg}"
            )
            return BumpResult(False, f"Тема {thread_id}: {error_msg}", thread_id, _error_status(error_msg))
        logger.info(f"Thread {thread_id} bumped successfully (job_result={job_result})")
        return BumpResult(True, f"✅ Тема {thread_id} поднята успешно", thread_id, BumpStatus.SUCCESS)

    if job_data.get("errors"):
        error_msg = extract_error_message(_errors_to_text(job_data["errors"])) or "Ошибка API"
        logger.error(f"Thread {thread_id} bump failed | Errors: {job_data['errors']} | Extracted: {error_msg}")
        return BumpResult(False, f"Тема {thread_id}: {error_msg}", thread_id, _error_status(error_msg))

    # Documented endpoint response: {"status": ..., "message": ..., "system_info": ...}
    status_value = job_data.get("status")
    if status_value is not None:
        if str(status_value).strip().lower() in SUCCESSFUL_STATUSES:
            logger.info(f"Thread {thread_id} bumped successfully (status={status_value})")
            return BumpResult(True, f"✅ Тема {thread_id} поднята успешно", thread_id, BumpStatus.SUCCESS)
        error_msg = extract_error_message(str(job_data.get("message", ""))) or f"Ошибка API (status={status_value})"
        logger.error(f"Thread {thread_id} bump failed | status={status_value} | message={error_msg}")
        return BumpResult(False, f"Тема {thread_id}: {error_msg}", thread_id, _error_status(error_msg))

    logger.warning(f"Thread {thread_id} unknown bump response (dict without recognized keys): {job_data}")
    return BumpResult(False, f"Тема {thread_id}: Неизвестный ответ (dict)", thread_id, BumpStatus.ERROR)


class APIClient:
    """Lolz API client with batch request support and connection pooling."""

    __slots__ = ("_base_url", "_auth_token", "_session", "_batch_size", "_batch_delay_seconds")

    def __init__(
        self,
        base_url: str,
        auth_token: str,
        batch_size: int = 10,
        batch_delay_seconds: float = 1.0,
    ) -> None:
        if not base_url or not auth_token:
            raise ValueError("base_url and auth_token are required")

        if batch_size < 1 or batch_size > 10:
            raise ValueError("batch_size must be between 1 and 10")

        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._session: aiohttp.ClientSession | None = None
        self._batch_size = batch_size
        self._batch_delay_seconds = max(0.0, float(batch_delay_seconds))

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Initialize HTTP session with connection pooling."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._auth_token}",
                "User-Agent": "AutoBumpBot/5.0",
                "Content-Type": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
                connector=connector,
            )

    async def close(self) -> None:
        """Close HTTP session and cleanup resources."""
        if self._session and not self._session.closed:
            await self._session.close()
            await asyncio.sleep(0.25)
            self._session = None

    def set_batch_size(self, batch_size: int) -> None:
        if batch_size < 1 or batch_size > 10:
            raise ValueError("batch_size must be between 1 and 10")
        self._batch_size = batch_size

    # ─── Low-level batch executor ──────────────────────────────

    @staticmethod
    async def _safe_json(resp: aiohttp.ClientResponse) -> object:
        try:
            return await resp.json(content_type=None)
        except Exception:
            return None

    @staticmethod
    def _rate_limit_wait_seconds(headers: object, body: object) -> float | None:
        """Estimate wait time from X-RateLimit-Reset / Retry-After / system_info.rate_limit."""
        now = time.time()
        candidates: list[float] = []

        if headers is not None:
            reset = headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    candidates.append(float(reset) - now)
                except (TypeError, ValueError):
                    pass
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    candidates.append(float(retry_after))
                except (TypeError, ValueError):
                    pass

        if isinstance(body, dict):
            system_info = body.get("system_info")
            rate_limit = system_info.get("rate_limit") if isinstance(system_info, dict) else None
            reset = rate_limit.get("reset") if isinstance(rate_limit, dict) else None
            if isinstance(reset, (int, float)) and reset > 0:
                candidates.append(float(reset) - now)

        positive = [c for c in candidates if c > 0]
        return min(positive) if positive else None

    async def _execute_batch(self, batch_payload: Sequence[dict]) -> dict:
        """POST /batch with retry and rate-limit handling. Returns the 'jobs' mapping."""
        if not self._session:
            await self.start()
        batch_url = f"{self._base_url}/batch"
        last_error = "unknown error"

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                async with self._session.post(batch_url, json=list(batch_payload)) as resp:
                    logger.info(
                        f"Batch API request ({len(batch_payload)} jobs), "
                        f"status: {resp.status}, attempt {attempt + 1}"
                    )

                    if resp.status == 200:
                        try:
                            body = await resp.json()
                        except Exception:
                            raise BatchRequestError("Неверный JSON в ответе batch API")
                        if isinstance(body, dict) and isinstance(body.get("jobs"), dict):
                            return body["jobs"]
                        raise BatchRequestError("Неверный формат ответа batch API (нет 'jobs')")

                    body = await self._safe_json(resp)

                    if resp.status == 401:
                        raise InvalidTokenError("Invalid API token - check your configuration")

                    if resp.status == 429:
                        wait = self._rate_limit_wait_seconds(resp.headers, body)
                        wait = wait if wait is not None else DEFAULT_429_WAIT_SECONDS
                        wait = min(wait, MAX_RATE_LIMIT_WAIT_SECONDS)
                        logger.warning(f"Batch rate-limited (429), waiting {wait:.0f}s (attempt {attempt + 1})")
                        await asyncio.sleep(wait)
                        continue

                    if resp.status == 400:
                        errors = body.get("errors") if isinstance(body, dict) else None
                        message = extract_error_message(_errors_to_text(errors)) if errors else ""
                        raise BatchRequestError(message or "Batch request rejected (HTTP 400)")

                    if 400 <= resp.status < 500:
                        raise BatchRequestError(f"HTTP {resp.status}: {str(body)[:200]}")

                    # 5xx and others are retryable
                    last_error = f"HTTP {resp.status}"

            except (InvalidTokenError, BatchRequestError):
                raise
            except aiohttp.ClientError as e:
                last_error = f"network error: {e}"

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                backoff = RETRY_DELAY_SECONDS * (attempt + 1)
                logger.warning(f"Batch attempt {attempt + 1} failed ({last_error}), retrying in {backoff}s")
                await asyncio.sleep(backoff)

        raise BatchRequestError(f"Batch failed after {MAX_RETRY_ATTEMPTS} attempts: {last_error}")

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> object:
        """GET request with retry and rate-limit handling. Returns parsed JSON body."""
        if not self._session:
            await self.start()
        url = f"{self._base_url}{path}"
        last_error = "unknown error"

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                async with self._session.get(url, params=params) as resp:
                    logger.info(f"GET {path} -> HTTP {resp.status}, attempt {attempt + 1}")

                    if resp.status == 200:
                        return await resp.json(content_type=None)

                    body = await self._safe_json(resp)

                    if resp.status == 401:
                        raise InvalidTokenError("Invalid API token - check your configuration")

                    if resp.status == 429:
                        wait = self._rate_limit_wait_seconds(resp.headers, body)
                        wait = wait if wait is not None else DEFAULT_429_WAIT_SECONDS
                        wait = min(wait, MAX_RATE_LIMIT_WAIT_SECONDS)
                        logger.warning(f"GET {path} rate-limited (429), waiting {wait:.0f}s")
                        await asyncio.sleep(wait)
                        continue

                    if 400 <= resp.status < 500:
                        raise BatchRequestError(f"GET {path} failed: HTTP {resp.status}")

                    last_error = f"HTTP {resp.status}"

            except (InvalidTokenError, BatchRequestError):
                raise
            except aiohttp.ClientError as e:
                last_error = f"network error: {e}"

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                backoff = RETRY_DELAY_SECONDS * (attempt + 1)
                logger.warning(f"GET {path} attempt {attempt + 1} failed ({last_error}), retrying in {backoff}s")
                await asyncio.sleep(backoff)

        raise BatchRequestError(f"GET {path} failed after {MAX_RETRY_ATTEMPTS} attempts: {last_error}")

    @staticmethod
    def _job_for(thread_id: str, jobs: dict, uri: str) -> object:
        """Look up a job result by explicit 'id' first, then fall back to URI key."""
        return jobs[thread_id] if thread_id in jobs else jobs.get(uri)

    # ─── Thread info (titles) ──────────────────────────────────

    async def get_thread_info(self, thread_id: str) -> ThreadInfo | None:
        """Get single thread information from API (legacy method, prefer batch methods)."""
        results = await self.get_threads_info_batch([thread_id])
        return results[0] if results else None

    async def get_threads_info_batch(self, thread_ids: Sequence[str]) -> list[ThreadInfo | None]:
        """Get multiple thread titles using batch API (up to batch_size per request)."""
        if not thread_ids:
            return []

        all_results: list[ThreadInfo | None] = []
        for i in range(0, len(thread_ids), self._batch_size):
            batch = thread_ids[i:i + self._batch_size]
            if all_results and self._batch_delay_seconds > 0:
                await asyncio.sleep(self._batch_delay_seconds)
            all_results.extend(await self._fetch_threads_info_batch(batch))
        return all_results

    async def _fetch_threads_info_batch(self, thread_ids: Sequence[str]) -> list[ThreadInfo | None]:
        if not thread_ids:
            return []

        payload = [
            {"id": str(thread_id), "method": "GET", "uri": f"{self._base_url}/threads/{thread_id}"}
            for thread_id in thread_ids
        ]
        try:
            jobs = await self._execute_batch(payload)
        except InvalidTokenError:
            raise
        except BatchRequestError as e:
            logger.error(f"Thread info batch failed: {e}")
            return [None] * len(thread_ids)

        results: list[ThreadInfo | None] = []
        for thread_id in thread_ids:
            uri = f"{self._base_url}/threads/{thread_id}"
            job = self._job_for(str(thread_id), jobs, uri)
            thread = job.get("thread") if isinstance(job, dict) else None
            if isinstance(thread, dict):
                results.append(ThreadInfo(thread_id=str(thread_id), title=str(thread.get("thread_title", "Unknown"))))
            else:
                logger.warning(f"Thread {thread_id}: no thread data in jobs response")
                results.append(None)
        return results

    # ─── My threads listing ────────────────────────────────────

    async def get_my_threads(self, page: int = 1, limit: int = 10) -> ThreadsPage:
        """List the token owner's threads: GET /threads?tab=mythreads."""
        body = await self._get_json(
            "/threads",
            params={"tab": "mythreads", "page": str(page), "limit": str(limit)},
        )
        if not isinstance(body, dict):
            raise BatchRequestError("Неверный формат ответа /threads")

        items: list[ThreadInfo] = []
        for thread in body.get("threads") or []:
            if not isinstance(thread, dict):
                continue
            items.append(ThreadInfo(
                thread_id=str(thread.get("thread_id", "")),
                title=str(thread.get("thread_title") or ""),
            ))

        try:
            total = int(body.get("threads_total") or len(items))
        except (TypeError, ValueError):
            total = len(items)
        return ThreadsPage(threads=items, total=total)

    # ─── Token validation ──────────────────────────────────────

    async def get_me(self) -> dict | None:
        """Validate the token via GET /users/me. Returns the user payload or None."""
        try:
            body = await self._get_json("/users/me")
        except (InvalidTokenError, BatchRequestError, ConnectionError) as e:
            logger.warning(f"get_me failed: {e}")
            return None
        if isinstance(body, dict):
            user = body.get("user")
            if isinstance(user, dict):
                return user
            return body
        return None

    # ─── Bump ──────────────────────────────────────────────────

    async def bump_thread(self, thread_id: str) -> BumpResult:
        """Bump single thread via API (legacy method, prefer bump_threads_batch)."""
        results = await self.bump_threads_batch([thread_id])
        return results[0]

    async def bump_threads_batch(self, thread_ids: Sequence[str]) -> list[BumpResult]:
        """Bump multiple threads using batch API (up to batch_size per request)."""
        if not thread_ids:
            return []

        all_results: list[BumpResult] = []
        for i in range(0, len(thread_ids), self._batch_size):
            batch = thread_ids[i:i + self._batch_size]
            if all_results and self._batch_delay_seconds > 0:
                await asyncio.sleep(self._batch_delay_seconds)
            all_results.extend(await self._execute_bump_batch(batch))
        return all_results

    async def _execute_bump_batch(self, thread_ids: Sequence[str]) -> list[BumpResult]:
        """Execute a single batch bump request with retry and rate-limit handling."""
        if not thread_ids:
            return []

        payload = [
            {"id": str(thread_id), "method": "POST", "uri": f"{self._base_url}/threads/{thread_id}/bump"}
            for thread_id in thread_ids
        ]
        logger.info(f"🚀 Executing bump batch request for {len(thread_ids)} threads: {list(thread_ids)}")

        try:
            jobs = await self._execute_batch(payload)
        except InvalidTokenError:
            return [
                BumpResult(False, f"Тема {tid}: Неверный токен API", tid, BumpStatus.UNAUTHORIZED)
                for tid in thread_ids
            ]
        except BatchRequestError as e:
            return [BumpResult(False, f"Тема {tid}: {e}", tid, BumpStatus.ERROR) for tid in thread_ids]
        except Exception as e:
            logger.error(f"Unexpected bump batch error: {e}", exc_info=True)
            return [
                BumpResult(False, f"Тема {tid}: Неожиданная ошибка - {e}", tid, BumpStatus.ERROR)
                for tid in thread_ids
            ]

        results: list[BumpResult] = []
        for thread_id in thread_ids:
            uri = f"{self._base_url}/threads/{thread_id}/bump"
            job = self._job_for(str(thread_id), jobs, uri)
            logger.info(f"Thread {thread_id} raw bump response: {job}")
            result = parse_bump_job_result(str(thread_id), job)
            log = logger.info if result.success else logger.error
            log(f"BUMP {'SUCCESS' if result.success else 'FAILED'} | Thread: {thread_id} | "
                f"Status: {result.status.value} | Message: {result.message}")
            results.append(result)
        return results
