from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# Chat must feel responsive: a slow model fails over to the next one instead of hanging the conversation.
DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=8.0)


class LLMError(RuntimeError):
    """The model couldn't be reached or returned something unusable."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # seconds the provider asked us to wait, if it said


def _retry_after(resp: httpx.Response) -> float | None:
    header = resp.headers.get("retry-after")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    try:  # Google puts it in the body: error.details[].retryDelay = "37s"
        for detail in resp.json().get("error", {}).get("details", []):
            m = re.fullmatch(r"([\d.]+)s", str(detail.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    except (ValueError, AttributeError):
        pass
    return None


async def post_json(client: httpx.AsyncClient, url: str, *, label: str, retry_delay: float, **kw: Any) -> dict[str, Any]:
    """POST with sensible retries for LLM APIs.

    - 5xx / connection errors: retry once (usually a blip)
    - 429 rate limit: no retry (it only burns more quota); the caller fails over and cools this model down
    - timeout: no retry (the next model is a better bet than waiting again)
    """
    last, status, after = "unknown error", None, None
    for attempt in range(2):
        try:
            resp = await client.post(url, **kw)
        except httpx.TimeoutException as exc:
            raise LLMError(f"{label} request failed ({exc.__class__.__name__})") from exc
        except httpx.HTTPError as exc:
            last, status = exc.__class__.__name__, None
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMError(f"{label} returned invalid JSON") from exc
            status, after = resp.status_code, _retry_after(resp)
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code not in (500, 502, 503, 504):
                break
        if attempt == 0:
            await asyncio.sleep(retry_delay)
    raise LLMError(f"{label} request failed ({last})", status=status, retry_after=after)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema subset: object / properties / required / enum


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # on assistant messages
    tool_call_id: str | None = None  # on tool messages
    name: str | None = None  # tool name, on tool messages
    raw: Any = None  # provider-specific payload to replay verbatim (e.g. Gemini thought signatures)
    raw_provider: str | None = None


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    raw_provider: str | None = None


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResult: ...
