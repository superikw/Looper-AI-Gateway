"""Validate supported fields before converting requests; never drop unknown options."""

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Function(StrictModel):
    name: str = Field(min_length=1)
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: StrictBool | None = None


class Tool(StrictModel):
    type: Literal["function"]
    function: Function


class ToolCallFunction(StrictModel):
    name: str = Field(min_length=1)
    arguments: str


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: ToolCallFunction


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_message(self):
        if self.role == "tool" and (not self.tool_call_id or self.content is None):
            raise ValueError("tool messages require tool_call_id and content")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool_calls")
        if self.content is None and not self.tool_calls:
            raise ValueError(
                "message content is required unless tool_calls are present"
            )
        if isinstance(self.content, list):
            for block in self.content:
                kind = block.get("type")
                if kind in ("text", "input_text", "output_text"):
                    if not isinstance(block.get("text"), str) or set(block) - {
                        "type",
                        "text",
                    }:
                        raise ValueError("text blocks require text and type only")
                elif kind == "image_url" and self.role == "user":
                    image = block.get("image_url")
                    if (
                        not isinstance(image, dict)
                        or not isinstance(image.get("url"), str)
                        or not image["url"]
                    ):
                        raise ValueError(
                            "image_url blocks require an image_url.url string"
                        )
                    if (
                        set(block) - {"type", "image_url"}
                        or set(image) - {"url", "detail"}
                        or image.get("detail", "auto") not in ("auto", "low", "high")
                    ):
                        raise ValueError("unsupported image_url options")
                else:
                    raise ValueError(f"unsupported content block: {kind}")
        return self


class StreamOptions(StrictModel):
    include_usage: StrictBool = False


class BaseProxyRequest(StrictModel):
    model: str = Field(min_length=1, strict=True)
    stream: StrictBool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    reasoning: dict[str, Any] | None = None
    metadata: dict[str, str] | None = None
    parallel_tool_calls: StrictBool | None = None
    store: Literal[False] = False


class ChatRequest(BaseProxyRequest):
    messages: list[Message] = Field(min_length=1)
    tools: list[Tool] | None = None
    tool_choice: Literal["auto", "none", "required"] | dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, gt=0, strict=True)
    max_completion_tokens: int | None = Field(default=None, gt=0, strict=True)
    response_format: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    stream_options: StreamOptions | None = None
    n: Literal[1] = 1

    @model_validator(mode="after")
    def validate_options(self):
        if (
            sum(
                value is not None
                for value in (
                    self.max_tokens,
                    self.max_completion_tokens,
                    self.max_output_tokens,
                )
            )
            > 1
        ):
            raise ValueError("specify only one token limit")
        if self.reasoning is not None and self.reasoning_effort is not None:
            raise ValueError("specify reasoning or reasoning_effort, not both")
        if isinstance(self.tool_choice, dict):
            choice = self.tool_choice
            function = choice.get("function")
            if (
                set(choice) != {"type", "function"}
                or choice["type"] != "function"
                or not isinstance(function, dict)
                or set(function) != {"name"}
                or not isinstance(function["name"], str)
                or not function["name"]
            ):
                raise ValueError("tool_choice must select a named function")
        if self.response_format is not None:
            fmt = self.response_format
            if fmt.get("type") in ("text", "json_object") and set(fmt) == {"type"}:
                pass
            elif (
                fmt.get("type") == "json_schema"
                and set(fmt) == {"type", "json_schema"}
                and isinstance(fmt["json_schema"], dict)
            ):
                schema = fmt["json_schema"]
                if (
                    not isinstance(schema.get("name"), str)
                    or not isinstance(schema.get("schema"), dict)
                    or set(schema) - {"name", "schema", "strict", "description"}
                ):
                    raise ValueError("invalid json_schema response format")
            else:
                raise ValueError("unsupported response_format")
        return self


class ResponsesRequest(BaseProxyRequest):
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    include: list[str] | None = None
    truncation: Literal["auto", "disabled"] | None = None

    @model_validator(mode="after")
    def validate_input(self):
        if not self.input:
            raise ValueError("input must not be empty")
        return self


def validate_proxy_payload(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if path == "/v1/embeddings":
        raise HTTPException(400, "embeddings are not supported in codex backend mode")
    schema = ChatRequest if path == "/v1/chat/completions" else ResponsesRequest
    try:
        return schema.model_validate(payload).model_dump(exclude_none=True)
    except ValidationError as exc:
        # Do not echo user content or credentials from pydantic's input values.
        errors = [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
            for e in exc.errors()
        ]
        raise HTTPException(400, errors) from None
