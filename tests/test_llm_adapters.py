import json

import httpx
import pytest

from app.assistant.llm.base import ChatMessage, LLMError, LLMResult, ToolCall, ToolSpec
from app.assistant.llm.fallback import FallbackProvider
from app.assistant.llm.factory import build_llm
from app.assistant.llm.gemini import GeminiProvider
from app.assistant.llm.openai import OpenAIProvider
from app.config import Settings
from tests.conftest import ScriptedLLM, TZ, at

TOOLS = [ToolSpec("create_task", "Create a task", {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})]


def body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# ----------------------------------------------------------------------------- Gemini
class TestGemini:
    def provider(self, http_mock, handler):
        client = http_mock(handler)
        return GeminiProvider("KEY", "gemini-test", client=client, retry_delay=0), client

    async def test_text_answer_and_request_shape(self, http_mock):
        p, client = self.provider(http_mock, lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Hi!"}]}}]}))
        result = await p.complete("be nice", [ChatMessage("user", "hello")], TOOLS)
        assert result.text == "Hi!" and result.tool_calls == []
        req = client.seen[0]
        assert req.url.path.endswith("/models/gemini-test:generateContent")
        assert req.headers["x-goog-api-key"] == "KEY" and "KEY" not in str(req.url)  # key in a header, never in the URL
        b = body(req)
        assert b["systemInstruction"]["parts"][0]["text"] == "be nice"
        assert b["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
        assert b["tools"][0]["functionDeclarations"][0]["name"] == "create_task"

    async def test_function_call_is_parsed_and_raw_preserved(self, http_mock):
        content = {"role": "model", "parts": [{"functionCall": {"name": "create_task", "args": {"title": "x"}}, "thoughtSignature": "sig"}]}
        p, _ = self.provider(http_mock, lambda r: httpx.Response(200, json={"candidates": [{"content": content}]}))
        result = await p.complete("s", [ChatMessage("user", "hi")], TOOLS)
        assert result.tool_calls == [ToolCall("call_0", "create_task", {"title": "x"})]
        assert result.raw == content and result.raw_provider == "gemini"

    async def test_history_with_tool_results_is_translated(self, http_mock):
        p, client = self.provider(http_mock, lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
        raw = {"role": "model", "parts": [{"functionCall": {"name": "create_task", "args": {"title": "A"}}, "thoughtSignature": "sig"}]}
        msgs = [
            ChatMessage("user", "add A and B"),
            ChatMessage("assistant", tool_calls=[ToolCall("call_0", "create_task", {"title": "A"}), ToolCall("call_1", "create_task", {"title": "B"})],
                        raw=raw, raw_provider="gemini"),
            ChatMessage("tool", json.dumps({"ok": True}), tool_call_id="call_0", name="create_task"),
            ChatMessage("tool", "plain text result", tool_call_id="call_1", name="create_task"),
        ]
        await p.complete("s", msgs, TOOLS)
        contents = body(client.seen[0])["contents"]
        assert contents[1] == raw  # replayed verbatim (thought signature intact)
        assert contents[2]["role"] == "user" and len(contents[2]["parts"]) == 2  # both responses in ONE turn
        assert contents[2]["parts"][0]["functionResponse"] == {"name": "create_task", "response": {"ok": True}}
        assert contents[2]["parts"][1]["functionResponse"]["response"] == {"result": "plain text result"}

    async def test_another_providers_history_is_rebuilt_not_replayed(self, http_mock):
        p, client = self.provider(http_mock, lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
        msgs = [ChatMessage("user", "x"), ChatMessage("assistant", "thinking", tool_calls=[ToolCall("id", "create_task", {"title": "A"})],
                                                       raw={"bogus": 1}, raw_provider="openai")]
        await p.complete("s", msgs, [])
        model_turn = body(client.seen[0])["contents"][1]
        assert model_turn["parts"] == [{"text": "thinking"}, {"functionCall": {"name": "create_task", "args": {"title": "A"}}}]
        assert "tools" not in body(client.seen[0])

    @pytest.mark.parametrize("payload", [{"candidates": []}, {"promptFeedback": {"blockReason": "SAFETY"}},
                                         {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}])
    async def test_empty_or_blocked_answers_raise(self, http_mock, payload):
        p, _ = self.provider(http_mock, lambda r: httpx.Response(200, json=payload))
        with pytest.raises(LLMError):
            await p.complete("s", [ChatMessage("user", "hi")], [])

    async def test_retries_once_on_server_error_then_succeeds(self, http_mock):
        calls = []

        def handler(r):
            calls.append(1)
            return httpx.Response(503, text="busy") if len(calls) == 1 else httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

        p, _ = self.provider(http_mock, handler)
        assert (await p.complete("s", [ChatMessage("user", "hi")], [])).text == "ok" and len(calls) == 2

    async def test_client_errors_are_not_retried_and_message_has_no_key(self, http_mock):
        calls = []

        def handler(r):
            calls.append(1)
            return httpx.Response(400, text="API key not valid")

        p, _ = self.provider(http_mock, handler)
        with pytest.raises(LLMError) as e:
            await p.complete("s", [ChatMessage("user", "hi")], [])
        assert len(calls) == 1 and "400" in str(e.value) and "KEY" not in str(e.value)

    async def test_network_errors_become_llm_errors(self, http_mock):
        def handler(r):
            raise httpx.ConnectError("no route")

        p, _ = self.provider(http_mock, handler)
        with pytest.raises(LLMError, match="ConnectError"):
            await p.complete("s", [ChatMessage("user", "hi")], [])


    async def test_quota_error_parses_googles_retry_delay(self, http_mock):
        body = {"error": {"code": 429, "message": "quota", "details": [{"@type": "x.RetryInfo", "retryDelay": "37s"}]}}
        p, client = self.provider(http_mock, lambda r: httpx.Response(429, json=body))
        with pytest.raises(LLMError) as e:
            await p.complete("s", [ChatMessage("user", "hi")], [])
        assert e.value.retry_after == 37.0 and len(client.seen) == 1

    async def test_timeouts_fail_fast_without_retry(self, http_mock):
        def handler(r):
            raise httpx.ReadTimeout("slow", request=r)

        p, client = self.provider(http_mock, handler)
        with pytest.raises(LLMError, match="ReadTimeout"):
            await p.complete("s", [ChatMessage("user", "hi")], [])
        assert len(client.seen) == 1


# ----------------------------------------------------------------------------- OpenAI
class TestOpenAI:
    def provider(self, http_mock, handler):
        client = http_mock(handler)
        return OpenAIProvider("sk-test", "gpt-test", client=client, retry_delay=0), client

    async def test_text_answer_and_request_shape(self, http_mock):
        p, client = self.provider(http_mock, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}}]}))
        result = await p.complete("sys", [ChatMessage("user", "hi")], TOOLS)
        assert result.text == "Hello"
        req = client.seen[0]
        assert req.headers["authorization"] == "Bearer sk-test"
        b = body(req)
        assert b["messages"][0] == {"role": "system", "content": "sys"} and b["model"] == "gpt-test"
        assert b["tools"][0]["function"]["name"] == "create_task"

    async def test_tool_calls_roundtrip(self, http_mock):
        payload = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "create_task", "arguments": '{"title": "x"}'}},
            {"id": "call_b", "type": "function", "function": {"name": "create_task", "arguments": "not json"}}]}}]}
        p, client = self.provider(http_mock, lambda r: httpx.Response(200, json=payload))
        result = await p.complete("s", [ChatMessage("user", "hi")], TOOLS)
        assert result.tool_calls == [ToolCall("call_a", "create_task", {"title": "x"}), ToolCall("call_b", "create_task", {})]

        msgs = [ChatMessage("user", "hi"), ChatMessage("assistant", tool_calls=result.tool_calls),
                ChatMessage("tool", '{"ok": true}', tool_call_id="call_a", name="create_task")]
        await p.complete("s", msgs, TOOLS)
        sent = body(client.seen[1])["messages"]
        assert sent[2]["tool_calls"][0] == {"id": "call_a", "type": "function", "function": {"name": "create_task", "arguments": '{"title": "x"}'}}
        assert sent[3] == {"role": "tool", "tool_call_id": "call_a", "content": '{"ok": true}'}

    @pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}])
    async def test_garbage_raises(self, http_mock, payload):
        p, _ = self.provider(http_mock, lambda r: httpx.Response(200, json=payload))
        with pytest.raises(LLMError):
            await p.complete("s", [ChatMessage("user", "hi")], [])

    async def test_rate_limit_is_not_retried_and_reports_retry_after(self, http_mock):
        calls = []

        def handler(r):
            calls.append(1)
            return httpx.Response(429, headers={"Retry-After": "12"}, json={"error": {"message": "slow down"}})

        p, _ = self.provider(http_mock, handler)
        with pytest.raises(LLMError) as e:
            await p.complete("s", [ChatMessage("user", "hi")], [])
        assert len(calls) == 1 and e.value.status == 429 and e.value.retry_after == 12.0


# ----------------------------------------------------------------------------- failover
class TestFallback:
    async def test_uses_first_working_provider(self):
        a, b = ScriptedLLM([LLMResult(text="A")]), ScriptedLLM([LLMResult(text="B")])
        assert (await FallbackProvider([a, b]).complete("s", [], [])).text == "A" and b.calls == []

    async def test_falls_through_on_error(self):
        a, b = ScriptedLLM([LLMError("down")]), ScriptedLLM([LLMResult(text="B")])
        assert (await FallbackProvider([a, b]).complete("s", [], [])).text == "B"

    async def test_raises_when_everything_fails(self):
        with pytest.raises(LLMError, match="x.*y|y.*x"):
            await FallbackProvider([ScriptedLLM([RuntimeError("x")]), ScriptedLLM([LLMError("y")])]).complete("s", [], [])


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestCooldown:
    async def test_rate_limited_provider_is_skipped_until_its_cooldown_ends(self):
        clock = FakeClock()
        a = ScriptedLLM([LLMError("quota", status=429, retry_after=30), LLMResult(text="A again")])
        b = ScriptedLLM([LLMResult(text="B1"), LLMResult(text="B2")])
        chain = FallbackProvider([a, b], clock=clock)
        assert (await chain.complete("s", [], [])).text == "B1"
        assert chain.cooling_down() == ["scripted"]
        assert (await chain.complete("s", [], [])).text == "B2" and len(a.calls) == 1  # skipped: no wasted request
        clock.t += 31
        assert (await chain.complete("s", [], [])).text == "A again"

    async def test_default_cooldowns(self):
        assert FallbackProvider._cooldown(LLMError("x", status=429)) == 60
        assert FallbackProvider._cooldown(LLMError("x", status=503)) == 20
        assert FallbackProvider._cooldown(LLMError("x", retry_after=9999)) == 300
        assert FallbackProvider._cooldown(RuntimeError("x")) == 20

    async def test_the_last_provider_is_never_skipped(self):
        clock = FakeClock()
        last = ScriptedLLM([LLMError("down"), LLMResult(text="back")])
        chain = FallbackProvider([last], clock=clock)
        with pytest.raises(LLMError):
            await chain.complete("s", [], [])
        assert (await chain.complete("s", [], [])).text == "back"


def test_factory_orders_providers_and_always_ends_with_rules():
    ctx = lambda: (at(9), TZ)  # noqa: E731

    def names(**kw):
        return [p.name for p in build_llm(Settings(_env_file=None, **kw), ctx).providers]

    assert names() == ["rules"]
    assert names(GEMINI_API_KEY="g", GEMINI_MODEL="m1") == ["gemini:m1", "rules"]
    assert names(GEMINI_API_KEY="g", GEMINI_MODEL="m1, m2 ,") == ["gemini:m1", "gemini:m2", "rules"]
    assert names(GEMINI_API_KEY="g", GEMINI_MODEL="m1", OPENAI_API_KEY="o") == ["gemini:m1", "openai", "rules"]
    assert names(GEMINI_API_KEY="g", GEMINI_MODEL="m1", OPENAI_API_KEY="o", LLM_PROVIDER="openai") == ["openai", "gemini:m1", "rules"]
    assert names(OPENAI_API_KEY="o", LLM_PROVIDER="gemini") == ["openai", "rules"]


async def test_assistant_degrades_to_rules_when_the_real_model_is_down(settings, clock, notifier, http_mock):
    """The whole point of the fallback chain: a dead provider must not mean a dead assistant."""
    from app.assistant.llm.rules import RuleBasedProvider
    from app.container import build_container

    dead = GeminiProvider("K", "m", client=http_mock(lambda r: httpx.Response(500, text="oops")), retry_delay=0)
    holder = {}
    c = build_container(settings, clock=clock, notifier=notifier,
                        llm=FallbackProvider([dead, RuleBasedProvider(lambda: (holder["c"].now(), holder["c"].tz))]))
    holder["c"] = c
    await c.start()
    try:
        reply = await c.assistant.handle("remind me to call mom tomorrow at 7pm")
        assert reply.text.startswith("Created reminder: Call mom")
    finally:
        await c.close()
