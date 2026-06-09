"""
响应处理器基类和通用工具
"""

import asyncio
import re
import time
from typing import Any, AsyncGenerator, Optional, AsyncIterable, List, TypeVar

import orjson

from app.core.config import get_config
from app.core.logger import logger
from app.core.exceptions import StreamIdleTimeoutError
from app.services.grok.utils.download import DownloadService


T = TypeVar("T")


def _is_http2_error(e: Exception) -> bool:
    """检查是否为 HTTP/2 流错误"""
    err_str = str(e).lower()
    return "http/2" in err_str or "curl: (92)" in err_str or "stream" in err_str


def _normalize_line(line: Any) -> Optional[str]:
    """规范化流式响应行，兼容 SSE data 前缀与空行"""
    if line is None:
        return None
    if isinstance(line, (bytes, bytearray)):
        text = line.decode("utf-8", errors="ignore")
    else:
        text = str(line)
    text = text.strip()
    if not text:
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if text == "[DONE]":
        return None
    return text


def _collect_images(obj: Any) -> List[str]:
    """递归收集响应中的图片 URL"""
    urls: List[str] = []
    seen = set()

    def add(url: str):
        if not url or url in seen:
            return
        seen.add(url)
        urls.append(url)

    def walk(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"generatedImageUrls", "imageUrls", "imageURLs"}:
                    if isinstance(item, list):
                        for url in item:
                            if isinstance(url, str):
                                add(url)
                    elif isinstance(item, str):
                        add(item)
                    continue
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return urls


def _is_preview_image_url(url: str) -> bool:
    lower = (url or "").lower()
    return (
        bool(re.search(r"(^|[-_/])part[-_/]?\d+($|[./?_#-])", lower))
        or any(
            marker in lower
            for marker in (
                "thumbnail",
                "thumb",
                "preview",
                "partial",
                "intermediate",
            )
        )
    )


def _only_final_image_urls(urls: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for url in urls:
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        if url in seen or _is_preview_image_url(url):
            continue
        seen.add(url)
        result.append(url)
    return result


def _card_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)) and raw:
        try:
            payload = orjson.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except orjson.JSONDecodeError:
            return {}
    return {}


def _generated_card_is_final(payload: dict) -> bool:
    card_type = payload.get("type")
    if card_type not in {"render_generated_image", "render_edited_image"}:
        return True
    chunk = payload.get("image_chunk")
    if not isinstance(chunk, dict):
        return False
    try:
        return float(chunk.get("progress") or 0) >= 100
    except (TypeError, ValueError):
        return False


def _card_attachment_image(card_attachment: Any) -> Optional[tuple[str, str]]:
    if not isinstance(card_attachment, dict):
        return None

    payload = _card_payload(
        card_attachment.get("jsonData") or card_attachment.get("json_data")
    )
    if payload and not _generated_card_is_final(payload):
        return None

    source = payload if payload else card_attachment
    image = source.get("image") if isinstance(source, dict) else None
    if not isinstance(image, dict):
        return None
    original = image.get("original")
    if not isinstance(original, str) or not original.strip():
        return None
    if _is_preview_image_url(original):
        return None
    title = image.get("title") or ""
    return str(title), original.strip()


def _collect_final_images(obj: Any) -> List[str]:
    return _only_final_image_urls(_collect_images(obj))


async def _with_idle_timeout(
    iterable: AsyncIterable[T], idle_timeout: float, model: str = ""
) -> AsyncGenerator[T, None]:
    """
    包装异步迭代器，添加空闲超时检测

    Args:
        iterable: 原始异步迭代器
        idle_timeout: 空闲超时时间(秒)，0 表示禁用
        model: 模型名称(用于日志)
    """
    if idle_timeout <= 0:
        async for item in iterable:
            yield item
        return

    iterator = iterable.__aiter__()

    async def _maybe_aclose(it):
        aclose = getattr(it, "aclose", None)
        if not aclose:
            return
        try:
            await aclose()
        except Exception:
            pass

    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            yield item
        except asyncio.TimeoutError:
            logger.warning(
                f"Stream idle timeout after {idle_timeout}s",
                extra={"model": model, "idle_timeout": idle_timeout},
            )
            await _maybe_aclose(iterator)
            raise StreamIdleTimeoutError(idle_timeout)
        except asyncio.CancelledError:
            await _maybe_aclose(iterator)
            raise
        except StopAsyncIteration:
            break


class BaseProcessor:
    """基础处理器"""

    def __init__(self, model: str, token: str = ""):
        self.model = model
        self.token = token
        self.created = int(time.time())
        self.app_url = get_config("app.app_url")
        self._dl_service: Optional[DownloadService] = None

    def _get_dl(self) -> DownloadService:
        """获取下载服务实例（复用）"""
        if self._dl_service is None:
            self._dl_service = DownloadService()
        return self._dl_service

    async def close(self):
        """释放下载服务资源"""
        if self._dl_service:
            await self._dl_service.close()
            self._dl_service = None

    async def process_url(self, path: str, media_type: str = "image") -> str:
        """处理资产 URL"""
        dl_service = self._get_dl()
        return await dl_service.resolve_url(path, self.token, media_type)


__all__ = [
    "BaseProcessor",
    "_with_idle_timeout",
    "_normalize_line",
    "_collect_images",
    "_collect_final_images",
    "_card_attachment_image",
    "_is_http2_error",
]
