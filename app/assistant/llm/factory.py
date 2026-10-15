from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

from app.assistant.llm.base import DEFAULT_TIMEOUT, LLMProvider
from app.assistant.llm.fallback import FallbackProvider
from app.assistant.llm.gemini import GeminiProvider
from app.assistant.llm.openai import OpenAIProvider
from app.assistant.llm.rules import RuleBasedProvider
from app.config import Settings

log = logging.getLogger(__name__)


def build_llm(settings: Settings, context: Callable[[], tuple[datetime, ZoneInfo]]) -> LLMProvider:
    """Preferred provider first, the other one as a backup, and the offline rules last so something always answers."""
    # GEMINI_MODEL may list several models ("a,b"): when one is overloaded the next is tried.
    gemini: list[LLMProvider] = []
    if settings.GEMINI_API_KEY:
        client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        for model in [m.strip() for m in settings.GEMINI_MODEL.split(",") if m.strip()]:
            provider = GeminiProvider(settings.GEMINI_API_KEY, model, client=client)
            provider.name = f"gemini:{model}"
            gemini.append(provider)
    openai: list[LLMProvider] = [OpenAIProvider(settings.OPENAI_API_KEY, settings.OPENAI_MODEL)] if settings.OPENAI_API_KEY else []
    providers = gemini + openai if settings.LLM_PROVIDER.lower() != "openai" else openai + gemini
    if not providers:
        log.warning("No LLM API key configured; using the basic rule-based assistant.")
    providers.append(RuleBasedProvider(context))
    return FallbackProvider(providers)
