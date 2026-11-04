"""Bounded failover delays; quota and billing failures need caller action."""

import asyncio
import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.config import settings

PERMANENT_CODES = {
    "insufficient_quota",
    "quota_exceeded",
    "usage_limit_reached",
    "billing_hard_limit_reached",
    "billing_not_active",
    "account_deactivated",
}


async def sleep_retry(delay):
    await asyncio.sleep(delay)


def retry_after(response):
    value = response.headers.get("retry-after")
    if not value:
        return 0.0
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


def permanent_failure(result):
    error = result.get("error") if isinstance(result, dict) else None
    return isinstance(error, dict) and any(
        isinstance(value, str) and value in PERMANENT_CODES
        for value in (error.get("code"), error.get("type"))
    )


class RetryBudget:
    def __init__(self):
        self.waited = 0.0

    def reserve(self, retry_count, minimum=0.0):
        maximum = settings.upstream_retry_max_delay_seconds
        if minimum > maximum:
            return None
        delay = min(
            maximum, settings.upstream_retry_base_delay_seconds * 2 ** (retry_count - 1)
        )
        delay = min(maximum, max(minimum, delay) * (1 + random.random() * 0.2))
        if self.waited + delay > settings.upstream_retry_max_wait_seconds:
            return None
        self.waited += delay
        return delay
