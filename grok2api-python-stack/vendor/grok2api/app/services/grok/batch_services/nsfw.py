"""
Batch NSFW service.
"""

import asyncio
from typing import Callable, Awaitable, Dict, Any, Optional

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import UpstreamException
from app.services.reverse.accept_tos import AcceptTosReverse
from app.services.reverse.nsfw_mgmt import NsfwMgmtReverse
from app.services.reverse.set_birth import SetBirthReverse
from app.services.reverse.utils.session import ResettableSession
from app.core.batch import run_batch


_NSFW_SEMAPHORE = None
_NSFW_SEM_VALUE = None
_NSFW_STEP_LOCK = asyncio.Lock()


def _upstream_status(err: UpstreamException) -> int:
    if err.details and "status" in err.details:
        return int(err.details["status"] or 0)
    return int(getattr(err, "status_code", 0) or 0)


def _get_nsfw_semaphore() -> asyncio.Semaphore:
    value = min(2, max(1, int(get_config("nsfw.concurrent"))))
    global _NSFW_SEMAPHORE, _NSFW_SEM_VALUE
    if _NSFW_SEMAPHORE is None or value != _NSFW_SEM_VALUE:
        _NSFW_SEM_VALUE = value
        _NSFW_SEMAPHORE = asyncio.Semaphore(value)
    return _NSFW_SEMAPHORE


class NSFWService:
    """NSFW 模式服务"""
    @staticmethod
    async def batch(
        tokens: list[str],
        mgr,
        *,
        on_item: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Batch enable NSFW."""
        batch_size = min(2, max(1, int(get_config("nsfw.batch_size"))))
        interval_seconds = max(0.0, float(get_config("nsfw.interval_seconds", 10)))
        retry_429_attempts = max(1, int(get_config("nsfw.retry_429_attempts", 3)))
        retry_429_delay_seconds = max(
            1.0, float(get_config("nsfw.retry_429_delay_seconds", 20))
        )

        async def _paced(call):
            async with _NSFW_STEP_LOCK:
                try:
                    last_error = None
                    for attempt in range(1, retry_429_attempts + 1):
                        try:
                            result = await call()
                            return result
                        except UpstreamException as e:
                            last_error = e
                            if (
                                _upstream_status(e) != 429
                                or attempt >= retry_429_attempts
                            ):
                                raise
                            delay = retry_429_delay_seconds * attempt
                            logger.warning(
                                f"NSFW upstream 429, retrying step after {delay:.0f}s "
                                f"({attempt}/{retry_429_attempts})"
                            )
                            await asyncio.sleep(delay)
                    if last_error:
                        raise last_error
                finally:
                    if interval_seconds:
                        await asyncio.sleep(interval_seconds)

        async def _enable(token: str):
            try:
                browser = get_config("proxy.browser")
                async with ResettableSession(impersonate=browser) as session:
                    async def _record_fail(err: UpstreamException, reason: str):
                        status = _upstream_status(err)
                        if status == 401:
                            await mgr.record_fail(token, status, reason)
                        return status

                    try:
                        async with _get_nsfw_semaphore():
                            await _paced(lambda: AcceptTosReverse.request(session, token))
                    except UpstreamException as e:
                        status = await _record_fail(e, "tos_auth_failed")
                        return {
                            "success": False,
                            "http_status": status,
                            "error": f"Accept ToS failed: {str(e)}",
                        }

                    try:
                        async with _get_nsfw_semaphore():
                            await _paced(lambda: SetBirthReverse.request(session, token))
                    except UpstreamException as e:
                        status = await _record_fail(e, "set_birth_auth_failed")
                        if status == 429:
                            logger.warning(
                                "Set birth date rate limited; continuing NSFW enable "
                                "because the account may already satisfy age requirements."
                            )
                        else:
                            return {
                                "success": False,
                                "http_status": status,
                                "error": f"Set birth date failed: {str(e)}",
                            }

                    try:
                        async with _get_nsfw_semaphore():
                            grpc_status = await _paced(lambda: NsfwMgmtReverse.request(session, token))
                        success = grpc_status.code in (-1, 0)
                    except UpstreamException as e:
                        status = await _record_fail(e, "nsfw_mgmt_auth_failed")
                        return {
                            "success": False,
                            "http_status": status,
                            "error": f"NSFW enable failed: {str(e)}",
                        }
                    if success:
                        await mgr.add_tag(token, "nsfw")
                    return {
                        "success": success,
                        "http_status": 200,
                        "grpc_status": grpc_status.code,
                        "grpc_message": grpc_status.message or None,
                        "error": None,
                    }
            except Exception as e:
                logger.error(f"NSFW enable failed: {e}")
                return {"success": False, "http_status": 0, "error": str(e)[:100]}

        return await run_batch(
            tokens,
            _enable,
            batch_size=batch_size,
            on_item=on_item,
            should_cancel=should_cancel,
        )


__all__ = ["NSFWService"]
