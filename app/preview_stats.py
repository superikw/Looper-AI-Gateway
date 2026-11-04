"""Bounded, content-free summaries of bytes accepted during pre-reading."""

import json
import re

_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")
_TYPE_PREFIX = re.compile(r'^\s*\{\s*"type"\s*:\s*"([^"\\]{1,96})"')
_KNOWN_TYPES = {
    "error",
    "response.created",
    "response.queued",
    "response.in_progress",
    "response.completed",
    "response.done",
    "response.failed",
    "response.incomplete",
    "response.cancelled",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_text.delta",
    "response.output_text.done",
    "response.refusal.delta",
    "response.refusal.done",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_text.delta",
    "response.reasoning_text.done",
    "response.web_search_call.in_progress",
    "response.web_search_call.searching",
    "response.web_search_call.completed",
    "response.file_search_call.in_progress",
    "response.file_search_call.searching",
    "response.file_search_call.completed",
}


def event_type(value):
    # Never copy arbitrary upstream strings into diagnostics, even type fields.
    return value if isinstance(value, str) and value in _KNOWN_TYPES else "unknown"


def partial_type_hint(buffer):
    prefix = buffer[:4096].decode("utf-8-sig", errors="replace")
    for line in prefix.splitlines():
        if line.startswith("data:"):
            match = _TYPE_PREFIX.match(line[5:])
            return event_type(match[1]) if match else "unknown"
    return "comment_prefix" if prefix.startswith(":") else "unknown"


class PreviewStats:
    """Inspect the existing bounded buffer only; forwarding never feeds this."""

    def __init__(self):
        self.buffer = b""
        self.format = None
        self.bytes_read = 0
        self.event_count = 0
        self.frame_count = 0
        self.max_frame_bytes = 0
        self.comment_bytes = 0
        self.frames = []
        self.frozen = None

    def feed(self, chunk, body_format):
        if self.frozen is not None:
            return
        self.bytes_read += len(chunk)
        self.format = body_format
        if body_format == "json":
            self.buffer = b""
            return
        self.buffer += chunk
        if body_format is None:
            return
        while (match := _SEPARATOR.search(self.buffer)) is not None:
            frame, self.buffer = (
                self.buffer[: match.start()],
                self.buffer[match.end() :],
            )
            size = match.end()
            lines = frame.decode("utf-8-sig", errors="replace").splitlines()
            data = "\n".join(
                line[5:].removeprefix(" ") for line in lines if line.startswith("data:")
            )
            if data == "[DONE]":
                kind = "done_marker"
            elif data:
                try:
                    value = json.loads(data)
                except (ValueError, RecursionError):
                    kind = "invalid_data"
                else:
                    if isinstance(value, dict):
                        self.event_count += 1
                        kind = event_type(value.get("type"))
                    else:
                        kind = "invalid_data"
            elif lines and all(line.startswith(":") for line in lines):
                kind = "comment"
                self.comment_bytes += size
            else:
                kind = "no_data"
            self.frame_count += 1
            self.max_frame_bytes = max(self.max_frame_bytes, size)
            if len(self.frames) < 16:
                self.frames.append({"type": kind, "bytes": size})

    def freeze(self):
        if self.frozen is None:
            self.frozen = {
                "format": self.format or "unknown",
                "bytes_read": self.bytes_read,
                "event_count": self.event_count,
                "frame_count": self.frame_count,
                "max_frame_bytes": self.max_frame_bytes,
                "comment_bytes": self.comment_bytes,
                "frames": self.frames,
                "frames_omitted": self.frame_count - len(self.frames),
                "incomplete_frame_bytes": len(self.buffer),
                "incomplete_frame_type_hint": partial_type_hint(self.buffer)
                if self.buffer
                else None,
            }
            self.buffer = b""
        return self.frozen
