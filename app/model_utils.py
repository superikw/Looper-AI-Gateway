from __future__ import annotations

import re

GPT_COMPACT_RE = re.compile(r"^gpt[-_ ]?(\d+(?:\.\d+)?(?:-[a-z0-9]+)*)$", re.IGNORECASE)


def canonicalize_model_name(model: str | None) -> str | None:
    if model is None:
        return None

    normalized = model.strip().lower()
    if not normalized:
        return None

    normalized = normalized.replace("_", "-").replace(" ", "-")
    match = GPT_COMPACT_RE.fullmatch(normalized)
    if match:
        return f"gpt-{match.group(1)}"
    return normalized


def normalize_model_list(models: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized_models: list[str] = []
    for model in models:
        normalized = canonicalize_model_name(model)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_models.append(normalized)
    return normalized_models
