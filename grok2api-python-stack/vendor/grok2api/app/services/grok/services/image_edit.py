"""
Grok image edit service.
"""

import asyncio
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterable, Dict, List, Union, Any

import orjson
from curl_cffi.requests.errors import RequestsError

from app.core.config import get_config
from app.core.exceptions import (
    AppException,
    ErrorType,
    UpstreamException,
    StreamIdleTimeoutError,
)
from app.core.logger import logger
from app.services.grok.utils.process import (
    BaseProcessor,
    _with_idle_timeout,
    _normalize_line,
    _collect_images,
    _is_http2_error,
)
from app.services.grok.utils.upload import UploadService
from app.services.grok.utils.retry import pick_token, rate_limited
from app.services.grok.utils.response import make_response_id, make_chat_chunk, wrap_image_content
from app.services.grok.services.chat import GrokChatService
from app.services.grok.utils.stream import wrap_stream_with_usage
from app.services.token import EffortType

_EDIT_UPSTREAM_MODEL = "grok-4"
_EDIT_UPSTREAM_MODE = "MODEL_MODE_AUTO"


@dataclass
class ImageEditResult:
    stream: bool
    data: Union[AsyncGenerator[str, None], List[str]]


def _normalize_grok_image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    lower = value.lower()
    if lower.startswith("data:image/"):
        return value
    if lower.startswith("http://") or lower.startswith("https://"):
        if any(
            part in lower
            for part in (
                "assets.grok.com",
                "imgen",
                "image",
                "img",
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".gif",
            )
        ):
            return value
        return None
    if value.startswith("/"):
        return f"https://assets.grok.com{value}"
    if "/" in value and any(
        part in lower
        for part in (
            "generated",
            "image",
            "assets",
            "grok",
            "file-attachments",
            "image-attachments",
        )
    ):
        return f"https://assets.grok.com/{value.lstrip('/')}"
    return None


def _walk_image_urls(value: Any) -> List[str]:
    urls: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if any(
                part in key_lower
                for part in (
                    "url",
                    "uri",
                    "path",
                    "original",
                    "thumbnail",
                    "image",
                    "asset",
                    "file",
                )
            ):
                url = _normalize_grok_image_url(item)
                if url:
                    urls.append(url)
                    continue
            urls.extend(_walk_image_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_walk_image_urls(item))
    elif isinstance(value, str):
        for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", value):
            url = _normalize_grok_image_url(match.group(1).strip())
            if url:
                urls.append(url)
        for match in re.finditer(r"https?://[^\s\"')<>]+", value):
            candidate = match.group(0).rstrip(".,;")
            lower = candidate.lower()
            if any(part in lower for part in ("image", "img", "grok", "asset", ".png", ".jpg", ".jpeg", ".webp")):
                url = _normalize_grok_image_url(candidate)
                if url:
                    urls.append(url)
        for match in re.finditer(
            r"(?<![A-Za-z0-9_/-])(?:generated|image|images|assets|file-attachments|image-attachments)/[^\s\"')<>]+",
            value,
            re.IGNORECASE,
        ):
            url = _normalize_grok_image_url(match.group(0).rstrip(".,;"))
            if url:
                urls.append(url)
    return urls


def _card_attachment_image_urls(card_attachment: Any) -> List[str]:
    if not isinstance(card_attachment, dict):
        return []
    raw = card_attachment.get("jsonData") or card_attachment.get("json_data") or "{}"
    try:
        if isinstance(raw, str):
            payload = orjson.loads(raw)
        elif isinstance(raw, (bytes, bytearray)):
            payload = orjson.loads(raw)
        elif isinstance(raw, dict):
            payload = raw
        else:
            payload = {}
    except Exception as exc:
        logger.debug(f"Failed to parse cardAttachment jsonData: {exc}")
        payload = {}

    urls: List[str] = []
    card_type = payload.get("type", "") if isinstance(payload, dict) else ""
    if card_type in ("render_generated_image", "render_edited_image"):
        chunk = payload.get("image_chunk") if isinstance(payload, dict) else None
        if isinstance(chunk, dict):
            progress = chunk.get("progress")
            if progress is None or float(progress or 0) >= 100:
                urls.extend(_walk_image_urls(chunk))
    elif card_type == "render_searched_image":
        image = payload.get("image") if isinstance(payload, dict) else None
        urls.extend(_walk_image_urls(image))

    if not urls:
        urls.extend(_walk_image_urls(payload))

    seen = set()
    result: List[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return _prefer_final_image_urls(result)


def _response_image_urls(value: Any) -> List[str]:
    urls = []
    urls.extend(_collect_images(value))
    urls.extend(_walk_image_urls(value))

    seen = set()
    result: List[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return _prefer_final_image_urls(result)


def _prefer_final_image_urls(urls: List[str]) -> List[str]:
    def is_intermediate(url: str) -> bool:
        lower = url.lower()
        is_partial = bool(re.search(r"(^|[-_/])part[-_/]?\d+($|[./?_#-])", lower))
        is_preview = any(marker in lower for marker in ("thumbnail", "thumb", "preview"))
        return is_partial or is_preview

    return [url for url in urls if not is_intermediate(url)]


class ImageEditService:
    """Image edit orchestration service."""

    @staticmethod
    def _build_request_overrides(n: int) -> Dict[str, Any]:
        return {
            "imageGenerationCount": max(1, int(n or 1)),
            "disableSearch": True,
            "enableNsfw": bool(get_config("image.nsfw")),
        }

    async def edit(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        prompt: str,
        images: List[str],
        n: int,
        response_format: str,
        stream: bool,
        chat_format: bool = False,
    ) -> ImageEditResult:
        if len(images) > 3:
            logger.info(
                "Image edit received %d references; using the most recent 3",
                len(images),
            )
            images = images[-3:]

        max_token_retries = max(int(get_config("retry.max_retry") or 3), 8)
        tried_tokens: set[str] = set()
        last_error: Exception | None = None

        for attempt in range(max_token_retries):
            preferred = token if attempt == 0 else None
            current_token = await pick_token(
                token_mgr, model_info.model_id, tried_tokens, preferred=preferred
            )
            if not current_token:
                if last_error:
                    if rate_limited(last_error):
                        raise AppException(
                            message="No available tokens. Please try again later.",
                            error_type=ErrorType.RATE_LIMIT.value,
                            code="rate_limit_exceeded",
                            status_code=429,
                        )
                    raise last_error
                raise AppException(
                    message="No available tokens. Please try again later.",
                    error_type=ErrorType.RATE_LIMIT.value,
                    code="rate_limit_exceeded",
                    status_code=429,
                )

            tried_tokens.add(current_token)
            try:
                image_references = await self._upload_images(images, current_token)
                tool_overrides: Dict[str, Any] = {
                    "gmailSearch": False,
                    "googleCalendarSearch": False,
                    "outlookSearch": False,
                    "outlookCalendarSearch": False,
                    "googleDriveSearch": False,
                }
                if stream:
                    response = await self._request_imagine_edit(
                        token=current_token,
                        prompt=prompt,
                        n=n,
                        image_references=image_references,
                        tool_overrides=tool_overrides,
                    )
                    processor = ImageStreamProcessor(
                        model_info.model_id,
                        current_token,
                        n=n,
                        response_format=response_format,
                        chat_format=chat_format,
                    )
                    return ImageEditResult(
                        stream=True,
                        data=wrap_stream_with_usage(
                            processor.process(response),
                            token_mgr,
                            current_token,
                            model_info.model_id,
                        ),
                    )

                images_out = await self._collect_images(
                    token=current_token,
                    prompt=prompt,
                    n=n,
                    response_format=response_format,
                    image_references=image_references,
                    tool_overrides=tool_overrides,
                )
                try:
                    effort = (
                        EffortType.HIGH
                        if (model_info and model_info.cost.value == "high")
                        else EffortType.LOW
                    )
                    await token_mgr.consume(current_token, effort)
                    logger.debug(
                        f"Image edit completed, recorded usage (effort={effort.value})"
                    )
                except Exception as e:
                    logger.warning(f"Failed to record image edit usage: {e}")
                return ImageEditResult(stream=False, data=images_out)

            except UpstreamException as e:
                last_error = e
                if rate_limited(e):
                    await token_mgr.mark_rate_limited(current_token)
                    logger.warning(
                        f"Token {current_token[:10]}... rate limited (429), "
                        f"trying next token (attempt {attempt + 1}/{max_token_retries})"
                    )
                    continue
                raise

        if last_error:
            if rate_limited(last_error):
                raise AppException(
                    message="No available tokens. Please try again later.",
                    error_type=ErrorType.RATE_LIMIT.value,
                    code="rate_limit_exceeded",
                    status_code=429,
                )
            raise last_error
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )

    async def _upload_images(
        self, images: List[str], token: str
    ) -> List[str]:
        image_references: List[str] = []
        upload_service = UploadService()
        try:
            for image in images:
                file_id, file_uri = await upload_service.upload_file(image, token)
                reference = self._normalize_uploaded_reference(file_id, file_uri)
                if reference:
                    image_references.append(reference)
        finally:
            await upload_service.close()

        if not image_references:
            raise AppException(
                message="Image upload failed",
                error_type=ErrorType.SERVER.value,
                code="upload_failed",
            )

        return image_references

    @staticmethod
    def _normalize_uploaded_reference(file_id: str, file_uri: str) -> str:
        value = (file_uri or "").strip() or (file_id or "").strip()
        if not value:
            return ""
        lower = value.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            return value
        if value.startswith("/"):
            return f"https://assets.grok.com{value}"
        if "/" in value:
            return f"https://assets.grok.com/{value.lstrip('/')}"
        return value

    @staticmethod
    def _build_image_edit_overrides(
        prompt: str, image_references: List[str], n: int
    ) -> Dict[str, Any]:
        payload = {
            "modelName": "imagine-image-edit",
            "temporary": True,
            "message": prompt,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "imageGenerationCount": max(1, int(n or 1)),
            "forceConcise": False,
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "isReasoning": False,
            "disableTextFollowUps": True,
            "disableMemory": False,
            "forceSideBySide": False,
            "responseMetadata": {
                "modelConfigOverride": {
                    "modelMap": {
                        "imageEditModelConfig": {
                            "imageReferences": image_references,
                        },
                        "imageEditModel": "imagine",
                    }
                }
            },
        }
        return {"__raw_payload": payload}

    async def _request_imagine_edit(
        self,
        *,
        token: str,
        prompt: str,
        n: int,
        image_references: List[str],
        tool_overrides: dict,
    ) -> AsyncIterable[bytes]:
        return await GrokChatService().chat(
            token=token,
            message=prompt,
            model=None,
            mode=None,
            stream=True,
            file_attachments=[],
            tool_overrides=tool_overrides,
            request_overrides=self._build_image_edit_overrides(
                prompt, image_references, n
            ),
        )

    async def _collect_images(
        self,
        *,
        token: str,
        prompt: str,
        n: int,
        response_format: str,
        image_references: List[str],
        tool_overrides: dict,
    ) -> List[str]:
        per_call = 1
        calls_needed = max(1, (n + per_call - 1) // per_call)

        async def _call_edit():
            response = await self._request_imagine_edit(
                token=token,
                prompt=prompt,
                n=per_call,
                image_references=image_references,
                tool_overrides=tool_overrides,
            )
            processor = ImageCollectProcessor(
                "grok-imagine-1.0-edit", token, response_format=response_format
            )
            return await processor.process(response)

        last_error: Exception | None = None
        rate_limit_error: Exception | None = None
        all_images: List[str] = []

        if calls_needed == 1:
            try:
                all_images = await _call_edit()
            except Exception as error:
                logger.warning(f"Image edit call failed: {error}")
                last_error = error
                if rate_limited(error):
                    rate_limit_error = error
        else:
            tasks = [_call_edit() for _ in range(calls_needed)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_images: List[str] = []
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Concurrent image edit call failed: {result}")
                    last_error = result
                    if rate_limited(result):
                        rate_limit_error = result
                elif isinstance(result, list):
                    all_images.extend(result)

        if not all_images:
            if rate_limit_error:
                raise rate_limit_error
            if last_error:
                raise last_error
            raise UpstreamException(
                "Image edit returned no results", details={"error": "empty_result"}
            )

        if len(all_images) >= n:
            return all_images[:n]

        selected_images = all_images.copy()
        while len(selected_images) < n:
            selected_images.append("error")
        return selected_images


class ImageStreamProcessor(BaseProcessor):
    """HTTP image stream processor."""

    def __init__(
        self, model: str, token: str = "", n: int = 1, response_format: str = "b64_json", chat_format: bool = False
    ):
        super().__init__(model, token)
        self.partial_index = 0
        self.n = n
        self.target_index = 0 if n == 1 else None
        self.response_format = response_format
        self.chat_format = chat_format
        self._id_generated = False
        self._response_id = ""
        self._image_ids: Dict[int, str] = {}  # imageIndex → generated image_id
        if response_format == "url":
            self.response_field = "url"
        elif response_format == "base64":
            self.response_field = "base64"
        else:
            self.response_field = "b64_json"

    def _get_image_id(self, image_index: int) -> str:
        """Get or create a stable image_id for a given image index."""
        if image_index not in self._image_ids:
            self._image_ids[image_index] = f"app-chat-{int(time.time() * 1000)}-{image_index}"
        return self._image_ids[image_index]

    def _sse(self, event: str, data: dict) -> str:
        """Build SSE response."""
        return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """Process stream response."""
        final_images = []
        emitted_chat_chunk = False
        idle_timeout = get_config("image.stream_timeout")

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                # Image generation progress
                if img := resp.get("streamingImageGenerationResponse"):
                    image_index = img.get("imageIndex", 0)
                    progress = img.get("progress", 0)
                    image_url = _normalize_grok_image_url(img.get("imageUrl"))

                    if self.n == 1 and image_index != self.target_index:
                        continue

                    out_index = 0 if self.n == 1 else image_index

                    if image_url and float(progress or 0) >= 100:
                        if image_url not in final_images:
                            if self.response_format == "url":
                                processed = await self.process_url(image_url, "image")
                                if processed:
                                    final_images.append(processed)
                            else:
                                try:
                                    dl_service = self._get_dl()
                                    base64_data = await dl_service.parse_b64(
                                        image_url, self.token, "image"
                                    )
                                    if base64_data:
                                        b64 = base64_data.split(",", 1)[1] if "," in base64_data else base64_data
                                        final_images.append(b64)
                                except Exception as e:
                                    logger.warning(f"Failed to convert stream image to base64: {e}")
                                    processed = await self.process_url(image_url, "image")
                                    if processed:
                                        final_images.append(processed)
                        continue

                    if not self.chat_format:
                        image_id = self._get_image_id(image_index)
                        yield self._sse(
                            "image_generation.partial_image",
                            {
                                "type": "image_generation.partial_image",
                                self.response_field: "",
                                "index": out_index,
                                "progress": progress,
                                "image_id": image_id,
                            },
                        )
                    continue

                # Handle cardAttachment-based image generation (new Grok format)
                if ca := resp.get("cardAttachment"):
                    try:
                        for url in _card_attachment_image_urls(ca):
                            if url in final_images:
                                continue
                            if self.response_format == "url":
                                processed = await self.process_url(url, "image")
                                if processed:
                                    final_images.append(processed)
                            else:
                                try:
                                    dl_service = self._get_dl()
                                    base64_data = await dl_service.parse_b64(
                                        url, self.token, "image"
                                    )
                                    if base64_data:
                                        b64 = base64_data.split(",", 1)[1] if "," in base64_data else base64_data
                                        final_images.append(b64)
                                except Exception as e:
                                    logger.warning(f"Failed to convert stream card image to base64: {e}")
                                    processed = await self.process_url(url, "image")
                                    if processed:
                                        final_images.append(processed)
                    except Exception as card_err:
                        logger.warning(f"cardAttachment stream processing error: {card_err}")

                # modelResponse (legacy format)
                if mr := resp.get("modelResponse"):
                    if urls := _response_image_urls(mr):
                        for url in urls:
                            if self.response_format == "url":
                                processed = await self.process_url(url, "image")
                                if processed:
                                    final_images.append(processed)
                                continue
                            try:
                                dl_service = self._get_dl()
                                base64_data = await dl_service.parse_b64(
                                    url, self.token, "image"
                                )
                                if base64_data:
                                    if "," in base64_data:
                                        b64 = base64_data.split(",", 1)[1]
                                    else:
                                        b64 = base64_data
                                    final_images.append(b64)
                            except Exception as e:
                                logger.warning(
                                    f"Failed to convert image to base64, falling back to URL: {e}"
                                )
                                processed = await self.process_url(url, "image")
                                if processed:
                                    final_images.append(processed)

            for index, img_data in enumerate(final_images):
                if self.n == 1:
                    if index != self.target_index:
                        continue
                    out_index = 0
                else:
                    out_index = index

                # Wrap in markdown format for chat
                output = img_data
                if self.chat_format and output:
                    output = wrap_image_content(output, self.response_format)

                if not self._id_generated:
                    self._response_id = make_response_id()
                    self._id_generated = True

                if self.chat_format:
                    # OpenAI ChatCompletion chunk format
                    emitted_chat_chunk = True
                    yield self._sse(
                        "chat.completion.chunk",
                        make_chat_chunk(
                            self._response_id,
                            self.model,
                            output,
                            index=out_index,
                            is_final=True,
                        ),
                    )
                else:
                    # Original image_generation format
                    image_id = self._get_image_id(out_index)
                    yield self._sse(
                        "image_generation.completed",
                        {
                            "type": "image_generation.completed",
                            self.response_field: img_data,
                            "index": out_index,
                            "image_id": image_id,
                            "stage": "final",
                            "usage": {
                                "total_tokens": 0,
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "input_tokens_details": {
                                    "text_tokens": 0,
                                    "image_tokens": 0,
                                },
                            },
                        },
                    )

            if self.chat_format:
                if not self._id_generated:
                    self._response_id = make_response_id()
                    self._id_generated = True
                if not emitted_chat_chunk:
                    yield self._sse(
                        "chat.completion.chunk",
                        make_chat_chunk(
                            self._response_id,
                            self.model,
                            "",
                            index=0,
                            is_final=True,
                        ),
                    )
                yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.debug("Image stream cancelled by client")
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Image stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if _is_http2_error(e):
                logger.warning(f"HTTP/2 stream error in image: {e}")
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Image stream request error: {e}")
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Image stream processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class ImageCollectProcessor(BaseProcessor):
    """HTTP image non-stream processor."""

    def __init__(self, model: str, token: str = "", response_format: str = "b64_json"):
        if response_format == "base64":
            response_format = "b64_json"
        super().__init__(model, token)
        self.response_format = response_format

    async def process(self, response: AsyncIterable[bytes]) -> List[str]:
        """Process and collect images."""
        images = []
        idle_timeout = get_config("image.stream_timeout")
        seen_resp_keys: set[str] = set()
        seen_model_keys: set[str] = set()
        seen_card_types: set[str] = set()
        seen_model_image_shapes: set[str] = set()
        seen_card_shapes: set[str] = set()
        line_count = 0

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                line_count += 1
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})
                if isinstance(resp, dict):
                    seen_resp_keys.update(str(key) for key in resp.keys())
                if img := resp.get("streamingImageGenerationResponse"):
                    if isinstance(img, dict):
                        seen_card_shapes.add(
                            "streamingImageGenerationResponse="
                            f"{sorted(str(key) for key in img.keys())};"
                            f"progress={img.get('progress')}"
                        )
                        progress = img.get("progress")
                        url = _normalize_grok_image_url(img.get("imageUrl"))
                        if url and float(progress or 0) >= 100 and url not in images:
                            if self.response_format == "url":
                                processed = await self.process_url(url, "image")
                                if processed:
                                    images.append(processed)
                            else:
                                try:
                                    dl_service = self._get_dl()
                                    base64_data = await dl_service.parse_b64(
                                        url, self.token, "image"
                                    )
                                    if base64_data:
                                        b64 = base64_data.split(",", 1)[1] if "," in base64_data else base64_data
                                        images.append(b64)
                                except Exception as e:
                                    logger.warning(f"Failed to convert streaming image to base64: {e}")
                                    processed = await self.process_url(url, "image")
                                    if processed:
                                        images.append(processed)
                # Handle cardAttachment-based image generation/edit (new Grok format)
                if ca := resp.get("cardAttachment"):
                    try:
                        raw = ca.get("jsonData") if isinstance(ca, dict) else None
                        if isinstance(raw, str) and raw.strip():
                            try:
                                payload = orjson.loads(raw)
                                if isinstance(payload, dict) and payload.get("type"):
                                    seen_card_types.add(str(payload.get("type")))
                                    chunk = payload.get("image_chunk")
                                    if isinstance(chunk, dict):
                                        seen_card_shapes.add(
                                            "chunk_keys="
                                            f"{sorted(str(key) for key in chunk.keys())};"
                                            f"progress={chunk.get('progress')}"
                                        )
                                    else:
                                        seen_card_shapes.add(
                                            f"payload_keys={sorted(str(key) for key in payload.keys())}"
                                        )
                            except Exception:
                                pass
                        for url in _card_attachment_image_urls(ca):
                            if url in images:
                                continue
                            if self.response_format == "url":
                                processed = await self.process_url(url, "image")
                                if processed:
                                    images.append(processed)
                            else:
                                try:
                                    dl_service = self._get_dl()
                                    base64_data = await dl_service.parse_b64(
                                        url, self.token, "image"
                                    )
                                    if base64_data:
                                        b64 = base64_data.split(",", 1)[1] if "," in base64_data else base64_data
                                        images.append(b64)
                                except Exception as e:
                                    logger.warning(f"Failed to convert card image to base64: {e}")
                                    processed = await self.process_url(url, "image")
                                    if processed:
                                        images.append(processed)
                    except Exception as card_err:
                        logger.warning(f"cardAttachment processing error: {card_err}")

                if mr := resp.get("modelResponse"):
                    if isinstance(mr, dict):
                        seen_model_keys.update(str(key) for key in mr.keys())
                        for key in ("generatedImageUrls", "imageEditUris", "imageAttachments", "fileUris"):
                            value = mr.get(key)
                            if isinstance(value, list):
                                item_types = sorted({type(item).__name__ for item in value[:5]})
                                seen_model_image_shapes.add(f"{key}:list[{len(value)}]:{item_types}")
                            elif value:
                                seen_model_image_shapes.add(f"{key}:{type(value).__name__}")
                    if urls := _response_image_urls(mr):
                        for url in urls:
                            if self.response_format == "url":
                                processed = await self.process_url(url, "image")
                                if processed:
                                    images.append(processed)
                                continue
                            try:
                                dl_service = self._get_dl()
                                base64_data = await dl_service.parse_b64(
                                    url, self.token, "image"
                                )
                                if base64_data:
                                    if "," in base64_data:
                                        b64 = base64_data.split(",", 1)[1]
                                    else:
                                        b64 = base64_data
                                    images.append(b64)
                            except Exception as e:
                                logger.warning(
                                    f"Failed to convert image to base64, falling back to URL: {e}"
                                )
                                processed = await self.process_url(url, "image")
                                if processed:
                                    images.append(processed)

        except asyncio.CancelledError:
            logger.debug("Image collect cancelled by client")
        except StreamIdleTimeoutError as e:
            logger.warning(f"Image collect idle timeout: {e}")
        except RequestsError as e:
            if _is_http2_error(e):
                logger.warning(f"HTTP/2 stream error in image collect: {e}")
            else:
                logger.error(f"Image collect request error: {e}")
        except Exception as e:
            logger.error(
                f"Image collect processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
        finally:
            await self.close()

            if not images:
                logger.warning(
                    "Image collect returned no results: "
                    f"lines={line_count} card_types={sorted(seen_card_types)} "
                    f"model_image_shapes={sorted(seen_model_image_shapes)} "
                    f"card_shapes={sorted(seen_card_shapes)}"
                )
            return images


__all__ = ["ImageEditService", "ImageEditResult"]
