import asyncio

import orjson

from app.services.grok.services.chat import CollectProcessor, StreamProcessor
from app.services.grok.services.responses import ResponsesService


def _json_line(payload: dict) -> bytes:
    return orjson.dumps(payload)


async def _iter_lines(lines):
    for line in lines:
        yield line


def _decode_sse_json(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return orjson.loads(chunk[6:])


def _chat_config(key, default=None):
    if key == "chat.stream_timeout":
        return 0
    if key == "app.filter_tags":
        return []
    return default


class _FakeDownloadService:
    async def render_image(self, url: str, token: str, img_id: str) -> str:
        return f"![image]({url})"

    async def close(self):
        pass


def test_collect_processor_returns_estimated_usage(monkeypatch):
    monkeypatch.setattr(
        "app.services.grok.services.chat.get_config", _chat_config
    )

    async def _run():
        processor = CollectProcessor("grok-4", prompt_tokens=17)
        result = await processor.process(
            _iter_lines(
                [
                    _json_line(
                        {
                            "result": {
                                "response": {
                                    "llmInfo": {"modelHash": "fp_test"},
                                    "modelResponse": {
                                        "responseId": "resp_collect",
                                        "message": "你好，世界",
                                    },
                                }
                            }
                        }
                    )
                ]
            )
        )
        assert result["usage"]["prompt_tokens"] == 17
        assert result["usage"]["completion_tokens"] > 0
        assert (
            result["usage"]["total_tokens"]
            == result["usage"]["prompt_tokens"] + result["usage"]["completion_tokens"]
        )

    asyncio.run(_run())


def test_stream_processor_final_chunk_has_usage(monkeypatch):
    monkeypatch.setattr(
        "app.services.grok.services.chat.get_config", _chat_config
    )

    async def _run():
        processor = StreamProcessor("grok-4", prompt_tokens=11)
        chunks = []
        async for chunk in processor.process(
            _iter_lines(
                [
                    _json_line(
                        {
                            "result": {
                                "response": {
                                    "responseId": "resp_stream",
                                    "llmInfo": {"modelHash": "fp_test"},
                                    "token": "Hello",
                                }
                            }
                        }
                    ),
                    _json_line(
                        {
                            "result": {
                                "response": {
                                    "responseId": "resp_stream",
                                    "token": " world",
                                }
                            }
                        }
                    ),
                ]
            )
        ):
            chunks.append(chunk)

        assert chunks[-1] == "data: [DONE]\n\n"
        final_payload = _decode_sse_json(chunks[-2])
        assert final_payload["choices"][0]["finish_reason"] == "stop"
        assert final_payload["usage"]["prompt_tokens"] == 11
        assert final_payload["usage"]["completion_tokens"] > 0
        assert (
            final_payload["usage"]["total_tokens"]
            == final_payload["usage"]["prompt_tokens"]
            + final_payload["usage"]["completion_tokens"]
        )

    asyncio.run(_run())


def test_stream_processor_filters_partial_image_edit_card(monkeypatch):
    monkeypatch.setattr(
        "app.services.grok.services.chat.get_config", _chat_config
    )
    monkeypatch.setattr(
        "app.services.grok.utils.process.BaseProcessor._get_dl",
        lambda self: _FakeDownloadService(),
    )

    async def _run():
        processor = StreamProcessor("grok-4")
        partial_url = "https://assets.grok.com/generated/partial.png"
        final_url = "https://assets.grok.com/generated/final.jpg"
        chunks = []
        async for chunk in processor.process(
            _iter_lines(
                [
                    _json_line(
                        {
                            "result": {
                                "response": {
                                    "cardAttachment": {
                                        "jsonData": orjson.dumps(
                                            {
                                                "type": "render_edited_image",
                                                "image_chunk": {"progress": 40},
                                                "image": {
                                                    "title": "partial",
                                                    "original": partial_url,
                                                },
                                            }
                                        ).decode()
                                    }
                                }
                            }
                        }
                    ),
                    _json_line(
                        {
                            "result": {
                                "response": {
                                    "cardAttachment": {
                                        "jsonData": orjson.dumps(
                                            {
                                                "type": "render_edited_image",
                                                "image_chunk": {"progress": 100},
                                                "image": {
                                                    "title": "final",
                                                    "original": final_url,
                                                },
                                            }
                                        ).decode()
                                    }
                                }
                            }
                        }
                    ),
                ]
            )
        ):
            chunks.append(chunk)

        contents = [
            _decode_sse_json(chunk)["choices"][0]["delta"].get("content", "")
            for chunk in chunks
            if chunk.startswith("data: {")
        ]
        assert not any(partial_url in content for content in contents)
        assert any(final_url in content for content in contents)

    asyncio.run(_run())


def test_collect_processor_filters_partial_render_card(monkeypatch):
    monkeypatch.setattr(
        "app.services.grok.services.chat.get_config", _chat_config
    )

    async def _run():
        processor = CollectProcessor("grok-4")
        partial_url = "https://assets.grok.com/generated/partial.png"
        final_url = "https://assets.grok.com/generated/final.jpg"
        result = await processor.process(
            _iter_lines(
                [
                    _json_line(
                        {
                            "result": {
                                "response": {
                                    "modelResponse": {
                                        "responseId": "resp_collect",
                                        "message": (
                                            '<grok:render card_id="partial_card"></grok:render>'
                                            '<grok:render card_id="final_card"></grok:render>'
                                        ),
                                        "cardAttachmentsJson": [
                                            orjson.dumps(
                                                {
                                                    "id": "partial_card",
                                                    "type": "render_edited_image",
                                                    "image_chunk": {"progress": 50},
                                                    "image": {
                                                        "title": "partial",
                                                        "original": partial_url,
                                                    },
                                                }
                                            ).decode(),
                                            orjson.dumps(
                                                {
                                                    "id": "final_card",
                                                    "type": "render_edited_image",
                                                    "image_chunk": {"progress": 100},
                                                    "image": {
                                                        "title": "final",
                                                        "original": final_url,
                                                    },
                                                }
                                            ).decode(),
                                        ],
                                    },
                                }
                            }
                        }
                    )
                ]
            )
        )
        content = result["choices"][0]["message"]["content"]
        assert partial_url not in content
        assert final_url in content

    asyncio.run(_run())


def test_responses_stream_completed_event_uses_chat_usage(monkeypatch):
    async def fake_chat_completions(**kwargs):
        async def _gen():
            yield (
                'data: {"id":"chatcmpl_test","object":"chat.completion.chunk","created":1,'
                '"model":"grok-4","choices":[{"index":0,"delta":{"role":"assistant","content":""},'
                '"logprobs":null,"finish_reason":null}]}\n\n'
            )
            yield (
                'data: {"id":"chatcmpl_test","object":"chat.completion.chunk","created":1,'
                '"model":"grok-4","choices":[{"index":0,"delta":{"content":"Hello"},'
                '"logprobs":null,"finish_reason":null}]}\n\n'
            )
            yield (
                'data: {"id":"chatcmpl_test","object":"chat.completion.chunk","created":1,'
                '"model":"grok-4","choices":[{"index":0,"delta":{},'
                '"logprobs":null,"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":13,"completion_tokens":5,"total_tokens":18,'
                '"prompt_tokens_details":{"cached_tokens":0,"text_tokens":13,"audio_tokens":0,"image_tokens":0},'
                '"completion_tokens_details":{"text_tokens":5,"audio_tokens":0,"reasoning_tokens":0}}}\n\n'
            )
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(
        "app.services.grok.services.responses.ChatService.completions",
        fake_chat_completions,
    )

    async def _run():
        stream = await ResponsesService.create(
            model="grok-4",
            input_value="hi",
            stream=True,
        )
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        completed_chunk = next(
            chunk for chunk in reversed(chunks) if "response.completed" in chunk
        )
        completed = orjson.loads(completed_chunk.split("data: ", 1)[1])
        usage = completed["response"]["usage"]
        assert usage["input_tokens"] == 13
        assert usage["output_tokens"] == 5
        assert usage["total_tokens"] == 18

    asyncio.run(_run())
