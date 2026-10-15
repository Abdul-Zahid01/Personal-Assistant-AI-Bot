from __future__ import annotations

import json
from typing import Any

import httpx

from app.assistant.llm.base import DEFAULT_TIMEOUT, ChatMessage, LLMError, LLMResult, ToolCall, ToolSpec, post_json


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient | None = None, retry_delay: float = 1.0,
                 base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self.retry_delay = retry_delay
        self.base_url = base_url.rstrip("/")

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResult:
        body: dict[str, Any] = {"model": self.model, "temperature": 0.2, "messages": self._messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                              "parameters": t.parameters}} for t in tools]
        data = await self._post(body)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("OpenAI returned an unexpected response") from exc

        calls: list[ToolCall] = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(id=c["id"], name=c["function"]["name"], args=args if isinstance(args, dict) else {}))
        text = (msg.get("content") or "").strip()
        if not text and not calls:
            raise LLMError("OpenAI returned an empty answer")
        return LLMResult(text=text, tool_calls=calls)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        return await post_json(self.client, f"{self.base_url}/chat/completions", label="OpenAI", retry_delay=self.retry_delay,
                               json=body, headers={"Authorization": f"Bearer {self.api_key}"})

    @staticmethod
    def _messages(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    item["tool_calls"] = [{"id": c.id, "type": "function",
                                           "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in m.tool_calls]
                out.append(item)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        return out
