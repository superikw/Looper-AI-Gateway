"""Bounded pre-output inspection. A timed-out read is retained, not cancelled."""

import asyncio
import re
import time

from app.protocol import ResponseAccumulator, decode_sse_data, response_failed

_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")


def retryable_failure(result):
    error = result.get("error")
    if not isinstance(error, dict):
        return False
    if any(result.get(key) for key in ("output", "output_text", "refusal")):
        return False
    return (
        error.get("code") in {"overloaded_error", "server_overloaded"}
        or str(error.get("message", "")).strip().lower()
        == "our servers are currently overloaded. please try again later."
    )


def lifecycle_only(event):
    if event.get("type") not in {"response.created", "response.in_progress"}:
        return False
    response = event.get("response") or {}
    return not any(
        response.get(key) for key in ("output", "output_text", "error", "refusal")
    )


class Preview:
    def __init__(self, response):
        self.reader = self._chunks(response).__aiter__()
        self.chunks = []
        self.pending = None
        self.exhausted = False
        self.boundary_reached = False

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
            buffer += chunk
            while (match := _SEPARATOR.search(buffer)) is not None:
                frame, buffer = buffer[: match.start()], buffer[match.end() :]
                lines = [
                    line[5:].removeprefix(" ")
                    for line in frame.decode("utf-8", errors="replace").splitlines()
                    if line.startswith("data:")
                ]
                event = decode_sse_data(lines)
                if event is None:
                    continue
                count += 1
                accumulator.add(event)
                if accumulator.terminal and response_failed(accumulator.result()):
                    return (
                        accumulator.result()
                        if retryable_failure(accumulator.result())
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
        for chunk in self.chunks:
            yield chunk
        self.chunks.clear()
        if self.pending is not None:
            chunk = await self.pending
            self.pending = None
            if chunk is None:
                self.exhausted = True
            else:
                yield chunk
        if not self.exhausted:
            async for chunk in self.reader:
                yield chunk

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        await self.reader.aclose()
        self.chunks.clear()
