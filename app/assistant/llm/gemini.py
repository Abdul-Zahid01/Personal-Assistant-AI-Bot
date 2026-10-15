from __future__ import annotations

import json
from typing import Any

import httpx

from app.assistant.llm.base import DEFAULT_TIMEOUT, ChatMessage, LLMError, LLMResult, ToolCall, ToolSpec, post_json


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient | None = None, retry_delay: float = 1.0):
        self.api_key = api_key
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self.retry_delay = retry_delay

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResult:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(messages),
            "generationConfig": {"temperature": 0.2},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        data = await post_json(self.client, url, label="Gemini", retry_delay=self.retry_delay, json=body,
                               headers={"x-goog-api-key": self.api_key})
        return self._parse(data)

    @staticmethod
    def _contents(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                contents.append({"role": "user", "parts": [{"text": m.content}]})
            elif m.role == "assistant":
                if m.raw is not None and m.raw_provider == "gemini":
                    contents.append(m.raw)  # replay exactly (keeps thought signatures intact)
                    continue
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                parts += [{"functionCall": {"name": c.name, "args": c.args}} for c in m.tool_calls]
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif m.role == "tool":
                try:
                    response = json.loads(m.content)
                except ValueError:
                    response = {"result": m.content}
                if not isinstance(response, dict):
                    response = {"result": response}
                part = {"functionResponse": {"name": m.name or "tool", "response": response}}
                if contents and contents[-1]["role"] == "user" and all("functionResponse" in p for p in contents[-1]["parts"]):
                    contents[-1]["parts"].append(part)  # parallel calls: one user turn holds all responses
                else:
                    contents.append({"role": "user", "parts": [part]})
        return contents

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResult:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned no answer ({reason})")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        calls = [
            ToolCall(id=f"call_{i}", name=p["functionCall"]["name"], args=p["functionCall"].get("args") or {})
            for i, p in enumerate(p for p in parts if "functionCall" in p)
        ]
        if not text and not calls:
            raise LLMError(f"Gemini returned an empty answer (finish: {candidates[0].get('finishReason')})")
        return LLMResult(text=text.strip(), tool_calls=calls, raw=content if calls else None, raw_provider="gemini")
