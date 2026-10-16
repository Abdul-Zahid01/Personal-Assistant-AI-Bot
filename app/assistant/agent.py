"""The conversation loop: user text -> LLM <-> tools -> reply."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.assistant.llm.base import ChatMessage, LLMError, ToolCall
from app.assistant.prompts import build_system_prompt
from app.assistant.tools import TOOLS, ToolContext, tool_specs
from app.db.models import Message
from app.messaging import Button, Reply

if TYPE_CHECKING:  # pragma: no cover
    from app.container import Container

log = logging.getLogger(__name__)

MAX_STEPS = 6
# Results the model usually needs to reason about before answering ("what should I do first?").
NEEDS_FOLLOW_UP = {"list_tasks"}
HISTORY = 24
LLM_DOWN = "I can't reach my language model right now. Please try again in a moment."


class Assistant:
    def __init__(self, container: "Container"):
        self.c = container

    async def handle(self, text: str, channel: str = "web") -> Reply:
        text = (text or "").strip()
        if not text:
            return Reply("Tell me what you need — a task, a reminder, or ask me to plan your day.")

        async with self.c.chat_lock:  # one conversation: never interleave two turns
            async with self.c.session_factory() as session:
                session.add(Message(role="user", content=text[:4000], channel=channel, created_at=self.c.now()))
                await session.commit()

                ctx = await self.c.make_context(session)
                history = (await session.execute(
                    select(Message).order_by(Message.id.desc()).limit(HISTORY)
                )).scalars().all()
                msgs = [ChatMessage(role=m.role, content=m.content) for m in reversed(history)]
                open_tasks = await ctx.tasks.list_tasks(status="open", limit=10)
                system = build_system_prompt(ctx.now, ctx.tz, ctx.prefs, open_tasks)

                reply = await self._loop(ctx, system, msgs)

                session.add(Message(role="assistant", content=reply.text, channel=channel, created_at=self.c.now(),
                                    buttons=[b.as_pair() for b in reply.buttons] or None))
                await session.commit()
        self.c.after_change()
        return reply

    async def _loop(self, ctx: ToolContext, system: str, msgs: list[ChatMessage]) -> Reply:
        buttons: list[Button] = []
        specs = tool_specs()
        for _ in range(MAX_STEPS):
            try:
                result = await self.c.llm.complete(system, msgs, specs)
            except LLMError as exc:
                log.error("All LLM providers failed: %s", exc)
                return Reply(LLM_DOWN)
            if not result.tool_calls:
                text = (result.text or "Done.").replace("**", "").replace("__", "")  # chat shows plain text, not markdown
                return Reply(text, buttons)

            msgs.append(ChatMessage(role="assistant", content=result.text, tool_calls=result.tool_calls,
                                    raw=result.raw, raw_provider=result.raw_provider))
            outputs = []
            for call in result.tool_calls:
                output = await self._run_tool(ctx, call)
                if output.get("buttons"):
                    buttons = output.pop("buttons")
                outputs.append(output)
                msgs.append(ChatMessage(role="tool", content=json.dumps(output, default=str), tool_call_id=call.id, name=call.name))

            fast = (self.c.settings.FAST_REPLIES and all(o.get("ok") for o in outputs)
                    and not any(c.name in NEEDS_FOLLOW_UP for c in result.tool_calls))
            if fast:  # the tools already said it precisely; skip a second LLM round-trip
                return Reply("\n".join(o["message"] for o in outputs), buttons)
        return Reply("I got stuck on that one. Could you rephrase it?", buttons)

    async def _run_tool(self, ctx: ToolContext, call: ToolCall) -> dict[str, Any]:
        entry = TOOLS.get(call.name)
        if entry is None:
            return {"ok": False, "error": f"Unknown tool '{call.name}'."}
        try:
            out = await entry[1](ctx, call.args or {})
            await ctx.session.commit()
            return out
        except ValueError as exc:  # TaskError, PrefError, ToolError, bad dates...: the user can fix these
            await ctx.session.rollback()
            return {"ok": False, "error": str(exc)}
        except Exception:
            await ctx.session.rollback()
            log.exception("Tool %s crashed", call.name)
            return {"ok": False, "error": "Something went wrong on my side. Please try again."}
