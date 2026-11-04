"""Bounded pre-output inspection. A timed-out read is retained, not cancelled."""

import asyncio
import json
import re
import time

from fastapi import HTTPException

from app.protocol import ResponseAccumulator, decode_sse_data, response_failed
from app.retry_policy import permanent_failure

_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")


def retryable_failure(result):
    error = result.get("error")
    if not isinstance(error, dict) or permanent_failure(result):
        return False
    if any(result.get(key) for key in ("output", "output_text", "refusal")):
        return False
    codes = {
        "overloaded_error",
        "server_overloaded",
        "server_is_overloaded",
        "unavailable",
        "service_unavailable",
        "server_unavailable",
        "temporarily_unavailable",
    }
    return any(
        isinstance(value, str) and value in codes
        for value in (error.get("code"), error.get("type"))
    ) or str(error.get("message", "")).strip().lower() in {
        "our servers are currently overloaded. please try again later.",
        "the service is temporarily unavailable. please try again later.",
    }


def lifecycle_only(event):
    if event.get("type") not in {"response.created", "response.in_progress"}:
        return False
    response = event.get("response") or {}
    if not isinstance(response, dict):
        return False
    return not any(
        response.get(key) for key in ("output", "output_text", "error", "refusal")
    )


def output_boundary(event):
    """Describe a replay boundary without retaining an event or its content."""
    response = event.get("response")
    if isinstance(response, dict) and any(
        response.get(key) for key in ("output", "output_text", "refusal")
    ):
        return "meaningful_output"
    kind = event.get("type")
    if isinstance(kind, str) and kind.startswith(
        (
            "response.output_",
            "response.content_part.",
            "response.refusal.",
            "response.function_call_arguments.",
            "response.reasoning_",
        )
    ):
        return "meaningful_output"
    if kind in {
        "response.completed",
        "response.done",
        "response.incomplete",
        "response.cancelled",
    }:
        return "terminal_event"
    return "unknown_event"


class Preview:
    def __init__(self, response, *, strict=True):
        self.strict = strict
        self.reader = self._chunks(response).__aiter__()
        self.chunks = []
        self.pending = None
        self.exhausted = False
        self.boundary_reached = False
        self.stop_reason = None
        self.elapsed_ms = None
        self.started = None
        self.failure_at = None
        self.body_format = (
            "sse"
            if "text/event-stream" in response.headers.get("content-type", "").lower()
            else None
        )
        self._format_prefix = b""

    def observe_format(self, chunk):
        if self.body_format is not None:
            return
        prefix = (self._format_prefix + chunk).lstrip(b" \t\r\n")
        # Only retain a possible split UTF-8 BOM, never an unbounded prefix.
        if prefix and b"\xef\xbb\xbf".startswith(prefix):
            self._format_prefix = prefix
            return
        prefix = prefix.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
        self._format_prefix = b""
        if prefix:
            self.body_format = "json" if prefix[:1] in (b"{", b"[") else "sse"

    async def _chunks(self, response):
        # Split available bytes without waiting to fill a chunk: lifecycle/text
        # events smaller than 1 KiB must still be delivered promptly.
        async for chunk in response.aiter_bytes():
            for offset in range(0, len(chunk), 1024):
                yield chunk[offset : offset + 1024]

    async def scan(self, max_events, max_bytes, max_seconds):
        self.started = time.monotonic()
        try:
            return await self._scan(max_events, max_bytes, max_seconds)
        finally:
            self.elapsed_ms = round((time.monotonic() - self.started) * 1000)
            self.stop_reason = self.stop_reason or "preview_interrupted"

    def failure(self, result):
        self.failure_at = self.failure_at or time.monotonic()
        if any(result.get(key) for key in ("output", "output_text", "refusal")):
            self.stop_reason = "meaningful_output"
        elif retryable_failure(result):
            self.stop_reason = "retryable_failure"
            return result
        else:
            self.stop_reason = "non_retryable_error"
        return None

    async def _scan(self, max_events, max_bytes, max_seconds):
        deadline = time.monotonic() + max_seconds
        buffer = b""
        count = size = 0
        accumulator = ResponseAccumulator()
        while count < max_events and size < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop_reason = "preview_timeout"
                break
            self.pending = asyncio.create_task(anext(self.reader, None))
            done, _ = await asyncio.wait({self.pending}, timeout=remaining)
            if not done:
                self.stop_reason = "preview_timeout"
                break
            chunk = self.pending.result()
            self.pending = None
            if chunk is None:
                self.exhausted = True
                self.stop_reason = "stream_ended"
                break
            self.chunks.append(chunk)
            size += len(chunk)
            self.observe_format(chunk)
            if self.body_format == "json":
                body = await self.read_body(
                    max_bytes, max(0, deadline - time.monotonic())
                )
                if body is not None:
                    try:
                        result = json.loads(body)
                    except ValueError:
                        self.stop_reason = "invalid_event"
                        return None
                    if isinstance(result, dict) and response_failed(result):
                        return self.failure(result)
                    self.stop_reason = "json_response"
                return None
            buffer += chunk
            while (match := _SEPARATOR.search(buffer)) is not None:
                frame, buffer = buffer[: match.start()], buffer[match.end() :]
                lines = [
                    line[5:].removeprefix(" ")
                    for line in frame.decode("utf-8-sig", errors="replace").splitlines()
                    if line.startswith("data:")
                ]
                try:
                    event = decode_sse_data(lines)
                except HTTPException:
                    self.stop_reason = "invalid_event"
                    if self.strict:
                        raise
                    return None
                if event is None:
                    continue
                count += 1
                if self.strict:
                    accumulator.add(event)
                    if accumulator.terminal and response_failed(accumulator.result()):
                        return self.failure(accumulator.result())
                else:
                    kind = event.get("type")
                    if kind in ("error", "response.failed"):
                        result = (
                            event.get("response")
                            if kind == "response.failed"
                            else {
                                "error": event.get("error")
                                or {
                                    "code": event.get("code"),
                                    "message": event.get("message"),
                                }
                            }
                        )
                        if isinstance(result, dict):
                            return self.failure(result)
                        self.stop_reason = "non_retryable_error"
                        self.failure_at = time.monotonic()
                        return None
                if not lifecycle_only(event):
                    self.stop_reason = output_boundary(event)
                    return None
                if count >= max_events:
                    self.boundary_reached = True
                    self.stop_reason = "preview_event_limit"
                    return None
        self.boundary_reached = not self.exhausted
        self.stop_reason = self.stop_reason or "preview_byte_limit"
        return None

    async def replay(self):
        self._format_prefix = b""
        for chunk in self.chunks:
            self.observe_format(chunk)
            yield chunk
        self.chunks.clear()
        if self.pending is not None:
            chunk = await self.pending
            self.pending = None
            if chunk is None:
                self.exhausted = True
            else:
                self.observe_format(chunk)
                yield chunk
        if not self.exhausted:
            async for chunk in self.reader:
                self.observe_format(chunk)
                yield chunk

    async def read_body(self, max_bytes, max_seconds):
        """Bounded HTTP error inspection with the same lossless replay path."""
        standalone = self.started is None
        if standalone:
            self.started = time.monotonic()
        try:
            return await self._read_body(max_bytes, max_seconds)
        finally:
            if standalone:
                self.elapsed_ms = round((time.monotonic() - self.started) * 1000)
                self.stop_reason = self.stop_reason or "preview_interrupted"

    async def _read_body(self, max_bytes, max_seconds):
        deadline = time.monotonic() + max_seconds
        size = sum(map(len, self.chunks))
        while size < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop_reason = "preview_timeout"
                break
            self.pending = asyncio.create_task(anext(self.reader, None))
            done, _ = await asyncio.wait({self.pending}, timeout=remaining)
            if not done:
                self.stop_reason = "preview_timeout"
                break
            chunk = self.pending.result()
            self.pending = None
            if chunk is None:
                self.exhausted = True
                self.stop_reason = "body_complete"
                return b"".join(self.chunks)
            self.chunks.append(chunk)
            size += len(chunk)
        self.boundary_reached = True
        self.stop_reason = self.stop_reason or "preview_byte_limit"
        return None

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        await self.reader.aclose()
        self.chunks.clear()
