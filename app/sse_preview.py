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


class Preview:
    def __init__(self, response, *, strict=True):
        self.strict = strict
        self.reader = self._chunks(response).__aiter__()
        self.chunks = []
        self.pending = None
        self.exhausted = False
        self.boundary_reached = False
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
        deadline = time.monotonic() + max_seconds
        buffer = b""
        count = size = 0
        accumulator = ResponseAccumulator()
        while count < max_events and size < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.pending = asyncio.create_task(anext(self.reader, None))
            done, _ = await asyncio.wait({self.pending}, timeout=remaining)
            if not done:
                break
            chunk = self.pending.result()
            self.pending = None
            if chunk is None:
                self.exhausted = True
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
                        return None
                    if (
                        isinstance(result, dict)
                        and response_failed(result)
                        and retryable_failure(result)
                    ):
                        return result
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
                    if self.strict:
                        raise
                    return None
                if event is None:
                    continue
                count += 1
                if self.strict:
                    accumulator.add(event)
                    if accumulator.terminal and response_failed(accumulator.result()):
                        return (
                            accumulator.result()
                            if retryable_failure(accumulator.result())
                            else None
                        )
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
                        return (
                            result
                            if isinstance(result, dict) and retryable_failure(result)
                            else None
                        )
                if not lifecycle_only(event):
                    return None
                if count >= max_events:
                    self.boundary_reached = True
                    return None
        self.boundary_reached = not self.exhausted
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
        deadline = time.monotonic() + max_seconds
        size = sum(map(len, self.chunks))
        while size < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.pending = asyncio.create_task(anext(self.reader, None))
            done, _ = await asyncio.wait({self.pending}, timeout=remaining)
            if not done:
                break
            chunk = self.pending.result()
            self.pending = None
            if chunk is None:
                self.exhausted = True
                return b"".join(self.chunks)
            self.chunks.append(chunk)
            size += len(chunk)
        self.boundary_reached = True
        return None

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        await self.reader.aclose()
        self.chunks.clear()
