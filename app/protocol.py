"""Chat/Responses conversion and incremental Responses event handling."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import settings
from app.model_utils import canonicalize_model_name


class OutputContent(BaseModel):
    type: str
    text: str | None = None
    refusal: str | None = None


class OutputItem(BaseModel):
    type: str
    content: list[OutputContent] | None = None
    name: str | None = None
    arguments: str | None = None
    call_id: str | None = None

    @model_validator(mode="after")
    def function_fields(self):
        if self.type == "function_call" and (
            not self.name or not self.call_id or self.arguments is None
        ):
            raise ValueError("incomplete function call")
        return self


class UpstreamUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    prompt_tokens: int = Field(default=0, ge=0, strict=True)
    completion_tokens: int = Field(default=0, ge=0, strict=True)
    total_tokens: int = Field(default=0, ge=0, strict=True)


class UpstreamResponse(BaseModel):
    id: str | None = None
    created_at: int | float | None = None
    status: str | None = None
    output: list[OutputItem] | None = None
    output_text: str | None = None
    usage: UpstreamUsage | None = None
    error: dict[str, Any] | str | None = None
    incomplete_details: dict[str, Any] | None = None


def validate_upstream_data(data: dict) -> None:
    try:
        UpstreamResponse.model_validate(data)
    except ValidationError:
        raise HTTPException(
            502, "upstream returned an invalid response structure"
        ) from None


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _convert_content_to_parts(
    content: Any, assistant: bool = False
) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "output_text" if assistant else "input_text", "text": content}]
    parts = []
    for block in content or []:
        if block["type"] == "image_url":
            image = block["image_url"]
            parts.append(
                {
                    "type": "input_image",
                    "image_url": image["url"],
                    "detail": image.get("detail", "auto"),
                }
            )
        else:
            parts.append(
                {
                    "type": "output_text" if assistant else "input_text",
                    "text": block["text"],
                }
            )
    return parts


def _convert_messages_to_input(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    items, instructions = [], []
    for message in messages:
        role, content = message["role"], message.get("content")
        if role in ("system", "developer"):
            instructions.extend(
                part["text"] for part in _convert_content_to_parts(content)
            )
        elif role == "tool":
            output = (
                content
                if isinstance(content, str)
                else "\n".join(
                    part["text"] for part in _convert_content_to_parts(content)
                )
            )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": output,
                }
            )
        else:
            if content is not None:
                items.append(
                    {
                        "type": "message",
                        "role": role,
                        "content": _convert_content_to_parts(
                            content, role == "assistant"
                        ),
                    }
                )
            for call in message.get("tool_calls") or []:
                items.append(
                    {"type": "function_call", "call_id": call["id"], **call["function"]}
                )
    return items, "\n".join(instructions) or None


def build_codex_request_body(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = {
        "model": canonicalize_model_name(payload.get("model")),
        "store": False,
        "stream": True,
    }
    if path == "/v1/chat/completions":
        body["input"], instructions = _convert_messages_to_input(payload["messages"])
        body["instructions"] = instructions or settings.default_instructions
        if payload.get("tools") is not None:
            body["tools"] = [
                {"type": "function", **tool["function"]} for tool in payload["tools"]
            ]
        if payload.get("tool_choice") is not None:
            choice = payload["tool_choice"]
            body["tool_choice"] = (
                {"type": "function", "name": choice["function"]["name"]}
                if isinstance(choice, dict)
                else choice
            )
        for name in ("max_tokens", "max_completion_tokens"):
            if payload.get(name) is not None:
                body["max_output_tokens"] = payload[name]
        if payload.get("reasoning_effort") is not None:
            body["reasoning"] = {"effort": payload["reasoning_effort"]}
        if payload.get("response_format") is not None:
            fmt = payload["response_format"]
            body["text"] = {
                "format": {"type": "json_schema", **fmt["json_schema"]}
                if fmt["type"] == "json_schema"
                else fmt
            }
    elif path == "/v1/responses":
        value = payload["input"]
        body["input"] = (
            [{"role": "user", "content": [{"type": "input_text", "text": value}]}]
            if isinstance(value, str)
            else value
        )
        body["instructions"] = (
            payload.get("instructions") or settings.default_instructions
        )
        for name in ("tools", "tool_choice", "text", "include", "truncation"):
            if payload.get(name) is not None:
                body[name] = payload[name]
    else:
        raise HTTPException(400, "unsupported codex backend path")
    for name in (
        "temperature",
        "max_output_tokens",
        "reasoning",
        "metadata",
        "parallel_tool_calls",
    ):
        if payload.get(name) is not None:
            body[name] = payload[name]
    body.setdefault("include", ["reasoning.encrypted_content"])
    return body


def decode_sse_data(lines: list[str]) -> dict[str, Any] | None:
    if not lines:
        return None
    data = "\n".join(lines)
    if data == "[DONE]":
        return None
    try:
        event = json.loads(data, parse_constant=reject_constant)
    except ValueError:
        raise HTTPException(502, "upstream returned a malformed SSE event") from None
    if not isinstance(event, dict):
        raise HTTPException(502, "upstream returned a non-object SSE event")
    return event


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events, data = [], []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") + [""]:
        if not line:
            event = decode_sse_data(data)
            if event is not None:
                events.append(event)
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    return events


class ResponseAccumulator:
    def __init__(self):
        self.response = {}
        self.items = {}
        self.text = []
        self.refusals = []
        self.terminal = False

    def add(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if "response" in event:
            if not isinstance(event["response"], dict):
                raise HTTPException(502, "upstream returned an invalid response event")
            validate_upstream_data(event["response"])
        if kind in (
            "response.output_text.delta",
            "response.refusal.delta",
            "response.function_call_arguments.delta",
        ) and not isinstance(event.get("delta"), str):
            raise HTTPException(502, "upstream returned an invalid text delta")
        if "output_index" in event and (
            type(event["output_index"]) is not int or event["output_index"] < 0
        ):
            raise HTTPException(502, "upstream returned an invalid output index")
        if "item" in event:
            try:
                # Added function calls may not have their arguments yet.
                item = event["item"]
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ValueError()
                if kind == "response.output_item.done":
                    OutputItem.model_validate(item)
            except (ValueError, ValidationError):
                raise HTTPException(
                    502, "upstream returned an invalid output item"
                ) from None
        if kind == "response.output_text.delta":
            self.text.append(event.get("delta", ""))
        elif kind == "response.refusal.delta":
            self.refusals.append(event.get("delta", ""))
        if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            self.items[event.get("output_index", len(self.items))] = event["item"]
        if kind in ("response.created", "response.in_progress") and isinstance(
            event.get("response"), dict
        ):
            self.response.update(event["response"])
        if kind in (
            "response.completed",
            "response.done",
            "response.incomplete",
            "response.failed",
        ):
            self.terminal = True
            self.response.update(event.get("response") or {})
            self.response["status"] = (event.get("response") or {}).get(
                "status", "completed"
            )
            if kind == "response.failed":
                self.response["status"] = "failed"
                self.response.setdefault(
                    "error", {"message": "upstream response failed"}
                )
            elif kind == "response.incomplete":
                self.response["status"] = "incomplete"
        if kind == "error":
            self.terminal = True
            self.response["status"] = "failed"
            self.response["error"] = event.get("error") or {
                "message": event.get("message", "upstream error"),
                "code": event.get("code"),
            }

    def result(self) -> dict[str, Any]:
        result = dict(self.response)
        if not self.terminal:
            result["error"] = {
                "message": "upstream stream ended before a terminal response event",
                "type": "upstream_error",
            }
            result["status"] = "failed"
        if not result.get("output") and self.items:
            result["output"] = [self.items[i] for i in sorted(self.items)]
        if not result.get("output") and self.refusals:
            result["output"] = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "".join(self.refusals)}],
                }
            ]
        if not result.get("output_text"):
            final_text = "".join(
                part.get("text", "")
                for item in result.get("output") or []
                if item.get("type") == "message"
                for part in item.get("content") or []
                if part.get("type") == "output_text"
            )
            if final_text or self.text:
                result["output_text"] = final_text or "".join(self.text)
        return result


def extract_sse_json(text: str) -> dict[str, Any]:
    accumulator = ResponseAccumulator()
    for event in parse_sse_events(text):
        accumulator.add(event)
    return accumulator.result()


def response_failed(data: dict[str, Any]) -> bool:
    return bool(data.get("error")) or data.get("status") in ("failed", "cancelled")


def normalize_codex_response_to_chat(
    data: dict[str, Any], model: str
) -> dict[str, Any]:
    text, refusal, calls = [], [], []
    for item in data.get("output") or []:
        if item.get("type") == "function_call":
            calls.append(
                {
                    "id": item.get("call_id", item.get("id")),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "refusal":
                    refusal.append(part.get("refusal", ""))
                elif part.get("type") in ("text", "output_text"):
                    text.append(part.get("text", ""))
    content = data.get("output_text") or "".join(text)
    message = {
        "role": "assistant",
        "content": content or (None if calls or refusal else ""),
    }
    if calls:
        message["tool_calls"] = calls
    if refusal:
        message["refusal"] = "".join(refusal)
    reason = "tool_calls" if calls else "stop"
    if data.get("status") == "incomplete":
        reason = (
            "length"
            if (data.get("incomplete_details") or {}).get("reason")
            == "max_output_tokens"
            else "content_filter"
        )
    usage = data.get("usage") or {}
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return {
        "id": data.get("id", "codex_response"),
        "object": "chat.completion",
        "created": int(data.get("created_at") or time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": int(usage.get("total_tokens") or prompt + completion),
        },
    }


def normalize_codex_response_to_responses(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    result["object"] = "response"
    result.setdefault("output", [])
    result.setdefault(
        "output_text",
        "".join(
            part.get("text", "")
            for item in result["output"]
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ),
    )
    return result
