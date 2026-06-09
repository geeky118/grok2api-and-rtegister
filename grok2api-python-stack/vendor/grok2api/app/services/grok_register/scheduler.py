"""Low-account scheduler for creating grok-register tasks."""

import asyncio
import time
from datetime import datetime
from typing import Any, Optional

import aiohttp

from app.core.config import get_config
from app.core.logger import logger
from app.core.storage import StorageError, get_storage
from app.services.token.manager import get_token_manager


DEFAULT_MIN_AVAILABLE_ACCOUNTS = 500
DEFAULT_CHECK_INTERVAL_SECONDS = 300
DEFAULT_TASK_COUNT = 100
DEFAULT_TRIGGER_COOLDOWN_SECONDS = 3600
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_POOL_NAMES = ["ssoBasic", "ssoSuper"]
ACTIVE_TASK_STATUSES = {"queued", "running", "stopping"}
AUTO_NOTE_MARKER = "auto:grok2api-low-account-watermark"
MANUAL_NOTE_MARKER = "manual:grok2api-token-page"

_task: Optional[asyncio.Task] = None
_last_trigger_at = 0.0


def _get_bool(key: str, default: bool) -> bool:
    value = get_config(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _get_int(key: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(get_config(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _get_str(key: str, default: str = "") -> str:
    value = get_config(key, default)
    return str(value or "").strip()


def _get_pool_names() -> list[str]:
    value = get_config("grok_register.pool_names", DEFAULT_POOL_NAMES)
    if isinstance(value, str):
        names = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        names = [str(item).strip() for item in value]
    else:
        names = []
    return [name for name in names if name] or list(DEFAULT_POOL_NAMES)


def _get_task_api_url() -> str:
    return _get_str(
        "grok_register.task_api_url",
        _get_str("grok_register.task_url", ""),
    )


async def _available_account_count(pool_names: list[str]) -> int:
    manager = await get_token_manager()
    await manager.reload_if_stale()
    return manager.count_available_tokens(pool_names)


async def _has_active_register_task(
    session: aiohttp.ClientSession,
    task_api_url: str,
) -> bool:
    try:
        async with session.get(task_api_url) as response:
            if response.status < 200 or response.status >= 300:
                logger.warning(
                    "grok-register scheduler: task list check failed with HTTP {}",
                    response.status,
                )
                return False
            payload = await response.json(content_type=None)
    except Exception as exc:
        logger.warning("grok-register scheduler: task list check failed: {}", exc)
        return False

    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        return False

    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").lower()
        if status in ACTIVE_TASK_STATUSES:
            return True
    return False


async def _create_register_task(
    session: aiohttp.ClientSession,
    task_api_url: str,
    *,
    available_count: int,
    threshold: int,
    task_count: int,
) -> dict[str, Any]:
    prefix = _get_str("grok_register.task_name_prefix", "grok2api-auto-register")
    name = f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "name": name,
        "count": task_count,
        "notes": (
            f"{AUTO_NOTE_MARKER}; available={available_count}; "
            f"threshold={threshold}"
        ),
    }

    async with session.post(task_api_url, json=payload) as response:
        text = await response.text()
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"create task failed: HTTP {response.status}, body={text[:500]}"
            )
        try:
            data = await response.json(content_type=None)
        except Exception:
            data = {"raw": text}

    logger.warning(
        "grok-register scheduler: created register task name={}, count={}, "
        "available={}, threshold={}",
        name,
        task_count,
        available_count,
        threshold,
    )
    return data if isinstance(data, dict) else {"data": data}


async def create_manual_task(task_count: int) -> dict[str, Any]:
    """Create a grok-register task on explicit admin request."""
    task_api_url = _get_task_api_url()
    if not task_api_url:
        return {"status": "missing_task_api_url"}

    task_count = max(1, int(task_count))
    timeout_seconds = _get_int(
        "grok_register.request_timeout_seconds",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        minimum=1,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        name = f"grok2api-manual-register-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        payload = {
            "name": name,
            "count": task_count,
            "notes": f"{MANUAL_NOTE_MARKER}; source=admin-token-page",
        }
        async with session.post(task_api_url, json=payload) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                return {
                    "status": "error",
                    "error": f"HTTP {response.status}: {text[:500]}",
                }
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {"raw": text}

    logger.warning(
        "grok-register manual trigger: created register task name={}, count={}",
        name,
        task_count,
    )
    return {
        "status": "triggered",
        "task": data.get("task", data) if isinstance(data, dict) else data,
    }


async def check_once() -> dict[str, Any]:
    """Check account availability and create a register task if needed."""
    global _last_trigger_at

    if not _get_bool("grok_register.enabled", False):
        return {"status": "disabled"}

    task_api_url = _get_task_api_url()
    if not task_api_url:
        return {"status": "missing_task_api_url"}

    threshold = _get_int(
        "grok_register.min_available_accounts",
        DEFAULT_MIN_AVAILABLE_ACCOUNTS,
        minimum=1,
    )
    task_count = _get_int(
        "grok_register.task_count",
        DEFAULT_TASK_COUNT,
        minimum=1,
    )
    cooldown_seconds = _get_int(
        "grok_register.trigger_cooldown_seconds",
        DEFAULT_TRIGGER_COOLDOWN_SECONDS,
        minimum=0,
    )
    timeout_seconds = _get_int(
        "grok_register.request_timeout_seconds",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        minimum=1,
    )
    pool_names = _get_pool_names()

    available_count = await _available_account_count(pool_names)
    if available_count >= threshold:
        return {
            "status": "enough_accounts",
            "available": available_count,
            "threshold": threshold,
        }

    now = time.monotonic()
    if (
        cooldown_seconds > 0
        and _last_trigger_at > 0
        and now - _last_trigger_at < cooldown_seconds
    ):
        return {
            "status": "cooldown",
            "available": available_count,
            "threshold": threshold,
            "retry_after_seconds": int(cooldown_seconds - (now - _last_trigger_at)),
        }

    storage = get_storage()
    lock_timeout = max(timeout_seconds + 5, 30)
    try:
        async with storage.acquire_lock("grok_register_scheduler", timeout=lock_timeout):
            available_count = await _available_account_count(pool_names)
            if available_count >= threshold:
                return {
                    "status": "enough_accounts",
                    "available": available_count,
                    "threshold": threshold,
                }

            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if _get_bool("grok_register.skip_if_active_task", True):
                    if await _has_active_register_task(session, task_api_url):
                        return {
                            "status": "active_register_task_exists",
                            "available": available_count,
                            "threshold": threshold,
                        }

                task_response = await _create_register_task(
                    session,
                    task_api_url,
                    available_count=available_count,
                    threshold=threshold,
                    task_count=task_count,
                )
                _last_trigger_at = time.monotonic()
                return {
                    "status": "triggered",
                    "available": available_count,
                    "threshold": threshold,
                    "task": task_response.get("task", task_response),
                }
    except StorageError as exc:
        logger.debug("grok-register scheduler: skipped, lock unavailable: {}", exc)
        return {
            "status": "lock_unavailable",
            "available": available_count,
            "threshold": threshold,
        }
    except Exception as exc:
        logger.error("grok-register scheduler: trigger failed: {}", exc)
        return {
            "status": "error",
            "available": available_count,
            "threshold": threshold,
            "error": str(exc),
        }


async def _scheduler_loop():
    logger.info("grok-register scheduler: started")
    while True:
        try:
            result = await check_once()
            if result.get("status") in {"triggered", "error"}:
                logger.info("grok-register scheduler: check result {}", result)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("grok-register scheduler: check failed: {}", exc)

        interval = _get_int(
            "grok_register.check_interval_seconds",
            DEFAULT_CHECK_INTERVAL_SECONDS,
            minimum=30,
        )
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break

    logger.info("grok-register scheduler: stopped")


def start():
    """Start the background scheduler."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_scheduler_loop())
    logger.info("grok-register scheduler: background task enabled")


def stop():
    """Stop the background scheduler."""
    global _task
    if _task is None:
        return
    _task.cancel()
    _task = None
    logger.info("grok-register scheduler: background task cancelled")
