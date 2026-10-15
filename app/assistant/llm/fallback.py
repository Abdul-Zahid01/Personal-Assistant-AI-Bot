from __future__ import annotations

import logging
import time
from typing import Callable

from app.assistant.llm.base import ChatMessage, LLMError, LLMProvider, LLMResult, ToolSpec

log = logging.getLogger(__name__)

RATE_LIMIT_COOLDOWN = 60.0  # 429 without a hint
FAILURE_COOLDOWN = 20.0  # overloaded / timeout / other errors
MAX_COOLDOWN = 300.0


class FallbackProvider:
    """Tries each provider in order; the first one that answers wins.

    A provider that just failed (rate limit, overload, timeout) is skipped for a cooldown period, so one struggling
    model doesn't add seconds of latency to every message. The last provider is never skipped: it's the safety net.
    """

    name = "fallback"

    def __init__(self, providers: list[LLMProvider], clock: Callable[[], float] = time.monotonic):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.clock = clock
        self._cool_until: dict[int, float] = {}

    def cooling_down(self) -> list[str]:
        now = self.clock()
        return [p.name for i, p in enumerate(self.providers) if self._cool_until.get(i, 0) > now]

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResult:
        errors: list[str] = []
        last = len(self.providers) - 1
        for i, provider in enumerate(self.providers):
            if i != last and self._cool_until.get(i, 0) > self.clock():
                continue
            try:
                result = await provider.complete(system, messages, tools)
                self._cool_until.pop(i, None)
                return result
            except Exception as exc:
                self._cool_until[i] = self.clock() + self._cooldown(exc)
                log.warning("LLM provider %s failed: %s", provider.name, exc)
                errors.append(f"{provider.name}: {exc}")
        raise LLMError("; ".join(errors) or "all providers are cooling down")

    @staticmethod
    def _cooldown(exc: Exception) -> float:
        if isinstance(exc, LLMError) and exc.retry_after:
            return min(MAX_COOLDOWN, exc.retry_after)
        if isinstance(exc, LLMError) and exc.status == 429:
            return RATE_LIMIT_COOLDOWN
        return FAILURE_COOLDOWN
