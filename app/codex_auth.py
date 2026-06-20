from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings


API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]+\b")
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


@dataclass
class CredentialCandidate:
    kind: str
    value: str
    path: str


class CodexAuthError(Exception):
    pass


class CodexAuthManager:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None

    @property
    def codex_home(self) -> Path:
        return Path(settings.codex_home).expanduser()

    @property
    def auth_file(self) -> Path:
        return self.codex_home / 'auth.json'

    @property
    def login_log_file(self) -> Path:
        return Path(settings.codex_login_log_file).expanduser()

    def codex_exists(self) -> bool:
        return bool(subprocess.run(['bash', '-lc', f'command -v {settings.codex_command} >/dev/null 2>&1'], check=False).returncode == 0)

    def start_login(self) -> dict[str, Any]:
        if self._proc and self._proc.poll() is None:
            return {
                'started': False,
                'running': True,
                'pid': self._proc.pid,
                'message': 'login process already running',
                'log_file': str(self.login_log_file),
            }

        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.login_log_file.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env['CODEX_HOME'] = str(self.codex_home)
        if settings.codex_auth_credentials_store:
            env['CODEX_AUTH_CREDENTIALS_STORE'] = settings.codex_auth_credentials_store

        cmd = settings.codex_login_command or f"{settings.codex_command} login"
        log_fp = self.login_log_file.open('a', encoding='utf-8')
        self._proc = subprocess.Popen(
            ['bash', '-lc', cmd],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        return {
            'started': True,
            'running': True,
            'pid': self._proc.pid,
            'command': cmd,
            'log_file': str(self.login_log_file),
            'auth_file': str(self.auth_file),
        }

    def login_status(self) -> dict[str, Any]:
        running = bool(self._proc and self._proc.poll() is None)
        exit_code = None if running or not self._proc else self._proc.poll()
        auth_state = self.read_auth_state(safe=True)
        return {
            'codex_exists': self.codex_exists(),
            'running': running,
            'pid': self._proc.pid if self._proc else None,
            'exit_code': exit_code,
            'auth_file': str(self.auth_file),
            'log_file': str(self.login_log_file),
            'auth_state': auth_state,
        }

    def read_auth_state(self, safe: bool = True) -> dict[str, Any]:
        path = self.auth_file
        if not path.exists():
            return {
                'exists': False,
                'credential_found': False,
            }
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except Exception as e:
            return {
                'exists': True,
                'credential_found': False,
                'parse_error': str(e),
            }

        candidate = extract_best_credential(obj)
        preview = None
        if candidate and safe:
            val = candidate.value
            preview = f"{val[:8]}...{val[-4:]}" if len(val) > 16 else '***'
        return {
            'exists': True,
            'credential_found': candidate is not None,
            'credential_kind': candidate.kind if candidate else None,
            'credential_path': candidate.path if candidate else None,
            'credential_preview': preview,
            'top_level_keys': list(obj.keys()) if isinstance(obj, dict) else None,
        }

    def load_bearer_token(self) -> str:
        path = self.auth_file
        if not path.exists():
            raise CodexAuthError(f'codex auth file not found: {path}')
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except Exception as e:
            raise CodexAuthError(f'failed to parse auth file: {e}') from e

        candidate = extract_best_credential(obj)
        if not candidate:
            raise CodexAuthError('no usable token found in codex auth file')
        return candidate.value


def _iter_values(node: Any, prefix: str = '$'):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_values(value, f'{prefix}.{key}')
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_values(value, f'{prefix}[{idx}]')
    else:
        yield prefix, node


PREFERRED_KEYS = {
    'api_key', 'apikey', 'openai_api_key', 'token', 'access_token', 'id_token', 'bearer_token'
}


def extract_best_credential(obj: Any) -> CredentialCandidate | None:
    best: CredentialCandidate | None = None
    for path, value in _iter_values(obj):
        if not isinstance(value, str) or not value.strip():
            continue
        key_name = path.split('.')[-1].replace(']','').replace('[','')
        stripped = value.strip()
        kind = None
        if API_KEY_RE.search(stripped):
            kind = 'api_key'
        elif JWT_RE.match(stripped):
            kind = 'jwt'
        elif key_name in PREFERRED_KEYS and len(stripped) > 20:
            kind = 'token'
        if kind:
            cand = CredentialCandidate(kind=kind, value=stripped, path=path)
            if best is None:
                best = cand
            elif best.kind != 'api_key' and cand.kind == 'api_key':
                best = cand
    return best


codex_auth_manager = CodexAuthManager()
