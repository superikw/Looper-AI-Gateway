from __future__ import annotations

from pydantic import BaseModel


class AuthModeUpdate(BaseModel):
    mode: str


class LoginStartResponse(BaseModel):
    started: bool
    running: bool
    pid: int | None = None
    command: str | None = None
    message: str | None = None
    log_file: str
    auth_file: str | None = None
