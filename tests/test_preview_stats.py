import json

import pytest

from app.preview_stats import PreviewStats


def wire(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


@pytest.mark.parametrize("chunk_size", [1, 17, 1024])
def test_sse_summary_counts_utf8_wire_bytes_and_never_copies_content(chunk_size):
    frames = [
        b": private-heartbeat\r\n\r\n",
        wire({"type": "response.created", "response": {"output": []}}),
        wire({"type": "response.output_text.delta", "delta": "private-你好"}),
        b"data: [DONE]\n\n",
        wire({"type": "private-token", "output": "private-conversation"}),
        b"data: not-json-private\n\n",
        b"event: private-event\nid: private-id\n\n",
    ]
    body = b"".join(frames)
    stats = PreviewStats()
    for offset in range(0, len(body), chunk_size):
        stats.feed(body[offset : offset + chunk_size], "sse")
    result = stats.freeze()
    assert result["bytes_read"] == len(body)
    assert result["event_count"] == 3
    assert result["frame_count"] == 7
    assert result["max_frame_bytes"] == max(map(len, frames))
    assert result["comment_bytes"] == len(frames[0])
    assert [frame["type"] for frame in result["frames"]] == [
        "comment",
        "response.created",
        "response.output_text.delta",
        "done_marker",
        "unknown",
        "invalid_data",
        "no_data",
    ]
    assert [frame["bytes"] for frame in result["frames"]] == list(map(len, frames))
    assert result["incomplete_frame_bytes"] == 0
    assert "private" not in json.dumps(result)
    assert "你好" not in json.dumps(result, ensure_ascii=False)


def test_many_comments_keep_exact_totals_and_bounded_frame_details():
    stats = PreviewStats()
    body = b": private heartbeat\n\n" * 500
    stats.feed(body, "sse")
    result = stats.freeze()
    assert result["frame_count"] == 500 and result["event_count"] == 0
    assert result["bytes_read"] == result["comment_bytes"] == len(body)
    assert len(result["frames"]) == 16 and result["frames_omitted"] == 484
    assert len(json.dumps(result)) < 2048


@pytest.mark.parametrize(
    "prefix,kind",
    [
        (b'data: {"type":"response.created","response":', "response.created"),
        (b'data: {"type":"private-token","response":', "unknown"),
        (b'data: {"nested":{"type":"response.created"},"text":', "unknown"),
        (b": private-heartbeat ", "comment_prefix"),
    ],
)
def test_partial_frame_reports_read_bytes_and_only_a_safe_type_hint(prefix, kind):
    stats = PreviewStats()
    body = prefix + b"x" * (65536 - len(prefix))
    stats.feed(body, "sse")
    result = stats.freeze()
    assert result["bytes_read"] == result["incomplete_frame_bytes"] == 65536
    assert result["frame_count"] == result["event_count"] == 0
    assert result["max_frame_bytes"] == 0
    assert result["incomplete_frame_type_hint"] == kind
    assert "private" not in json.dumps(result)
    assert stats.buffer == b""
    stats.feed(b"\n\n", "sse")
    assert stats.freeze() == result


def test_json_is_not_counted_as_sse_even_if_its_strings_contain_event_text():
    stats = PreviewStats()
    body = json.dumps({"output_text": "data: secret\n\n", "type": "private"}).encode()
    stats.feed(b"\n\n", None)
    stats.feed(body, "json")
    result = stats.freeze()
    assert result["format"] == "json" and result["bytes_read"] == len(body) + 2
    assert result["frames"] == [] and result["incomplete_frame_bytes"] == 0
    assert result["frame_count"] == result["event_count"] == 0
    assert "secret" not in json.dumps(result)
