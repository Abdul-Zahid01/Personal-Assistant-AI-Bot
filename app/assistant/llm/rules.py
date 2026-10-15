"""Offline fallback "LLM": understands a handful of plain commands with regexes.

It speaks the same interface as the real models (it emits tool calls), so the agent loop, tools and tests are
identical whether the real model is up or not. Quality is intentionally modest — it keeps the assistant
useful when there's no API key, no network, or the provider is down.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.assistant.llm.base import ChatMessage, LLMResult, ToolCall, ToolSpec
from app.domain.timeutil import parse_when

HELP = (
    "I'm running without my language model right now, so I only understand simple commands:\n"
    "• “remind me to call mom tomorrow at 7pm”   • “add task write report by friday, 2 hours, urgent”\n"
    "• “plan my day” / “plan tomorrow”   • “what's on today?”   • “show tasks”\n"
    "• “done” / “done with report”   • “snooze 30 min”   • “end of day review”"
)

_EVENT_WORDS = r"meeting|appointment|call|gym|workout|session|interview|lecture|class|dinner|lunch"
_WEEKDAY = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
_STRIP = [
    r"^\s*(please\s+)?(remind me (to|about)\s+|(add|create|make|new)\s+(a\s+|an\s+)?(new\s+)?(task|todo|reminder|event)\s*(to|for|:|-)?\s*|todo:?\s*|task:?\s*|schedule\s+(a\s+)?)",
    r"\bin\s+\d+\s*(minutes?|mins?|hours?|hrs?|days?)\b",
    r"\b(by|on|at|for)?\s*\b(tomorr\w*|today|tonight)\b",
    rf"\b(on\s+|by\s+)?\b({_WEEKDAY})\b",
    r"\b(at|by)?\s*\d{1,2}(:\d{2})?\s*(am|pm)\b",
    r"\b(at|by)\s+\d{1,2}:\d{2}\b",
    r"\b(for\s+)?\d+\s*(minutes?|mins?|hours?|hrs?)\b",
    r"\b(urgent(ly)?|asap|high priority|low priority|medium priority|p[1-4])\b",
    r"\b(every\s+(day|week|weekday|month)|daily|weekly|monthly|weekdays)\b",
]


class RuleBasedProvider:
    name = "rules"

    def __init__(self, context: Callable[[], tuple[datetime, ZoneInfo]]):
        self.context = context

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResult:
        if messages and messages[-1].role == "tool":
            return LLMResult(text=self._summarise(messages))
        text = (messages[-1].content if messages else "").strip()
        call = self._route(text)
        return LLMResult(tool_calls=[call]) if call else LLMResult(text=HELP)

    # ------------------------------------------------------------------
    @staticmethod
    def _summarise(messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        for m in reversed(messages):
            if m.role != "tool":
                break
            try:
                data = json.loads(m.content)
            except ValueError:
                data = {"message": m.content}
            parts.append(data.get("message") if data.get("ok") else f"Sorry — {data.get('error', 'something went wrong')}")
        return "\n".join(reversed(parts))

    def _call(self, name: str, **args: Any) -> ToolCall:
        return ToolCall(id="rule_0", name=name, args={k: v for k, v in args.items() if v is not None})

    def _route(self, text: str) -> ToolCall | None:
        now, tz = self.context()
        t = re.sub(r"\s+", " ", text.lower()).strip().rstrip(".!?")
        if not t:
            return None

        m = re.match(r"^(?:i(?:'ve| have)?\s+)?(?:just\s+|already\s+)?(?:done|finished|completed|complete)\b\s*(?:with\s+)?(?:(?:the|my|task)\s+|:\s*)*(.*)$", t)
        if m:
            rest, actual = m.group(1), None
            took = re.search(r"[,;-]?\s*(?:it\s+)?took\s+(?:me\s+)?(?:about\s+)?(\d+)\s*(hours?|hrs?|h\b|minutes?|mins?|m\b)", rest)
            if took:
                actual = int(took.group(1)) * (60 if took.group(2).startswith("h") else 1)
                rest = rest[:took.start()]
            return self._call("complete_task", task_ref=rest.strip(" ,.;:-") or "latest", actual_min=actual)

        if re.match(r"^(snooze|postpone|delay)\b", t):
            n = re.search(r"(\d+)\s*(hours?|hrs?|h\b|minutes?|mins?|m\b)?", t)
            minutes = 30
            if n:
                minutes = int(n.group(1)) * (60 if (n.group(2) or "").startswith("h") else 1)
            return self._call("snooze_task", task_ref="latest", minutes=minutes)

        m = re.match(r"^(start|begin)\s+(.+)$", t)
        if m:
            return self._call("start_task", task_ref=m.group(2))

        m = re.match(r"^(delete|remove)\s+(?:the\s+)?(?:task\s+)?(.+)$", t)
        if m:
            return self._call("delete_task", task_ref=m.group(2))

        if re.search(r"\b(review|end of day|eod|day summary)\b", t):
            return self._call("end_of_day_review")

        day = "tomorrow" if "tomorrow" in t else "today"
        if re.search(r"\bplan\b", t) or re.match(r"^(schedule my|organi[sz]e my)\b", t):
            commit = bool(re.search(r"\b(confirm|approve|save|commit|go ahead)\b", t))
            return self._call("plan_day", date=day, commit=commit)

        if re.search(r"\b(show|list|what|give)\b.*\b(tasks?|todos?|reminders?)\b", t) and "schedule" not in t and "today" not in t:
            return self._call("list_tasks")
        if re.search(r"\b(schedule|agenda|today|tomorrow)\b", t) and re.search(r"\b(what|show|give|list|my)\b", t):
            return self._call("get_schedule", date=day)
        if re.search(r"\b(show|list|give)\b.*\b(tasks?|todo)\b", t):
            return self._call("list_tasks")

        if re.match(r"^(remind|add|create|new|todo|task|schedule|book|set up)\b", t) or re.search(r"\b(task|remind(er)?)\b", t) \
                or re.search(rf"\b({_EVENT_WORDS})\b", t):
            return self._create(text, t, now, tz)
        return None

    def _create(self, original: str, t: str, now: datetime, tz: ZoneInfo) -> ToolCall:
        due = parse_when(t, now, tz)
        minutes_text = re.sub(r"\bin\s+\d+\s*(minutes?|mins?|hours?|hrs?)\b", "", t)
        est = re.search(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", minutes_text)
        estimate = int(est.group(1)) * (60 if est.group(2).startswith("h") else 1) if est else None

        if re.search(r"\b(urgent(ly)?|asap|high priority|p1)\b", t):
            priority = "P1"
        elif re.search(r"\b(low priority|p4|whenever|no rush)\b", t):
            priority = "P4"
        elif re.search(r"\b(medium priority|p2)\b", t):
            priority = "P2"
        else:
            priority = "P3"

        recurrence = None
        if re.search(r"\b(weekdays|every weekday)\b", t):
            recurrence = "WEEKDAYS"
        elif re.search(r"\b(every day|daily)\b", t):
            recurrence = "DAILY"
        elif re.search(r"\b(every week|weekly)\b", t):
            recurrence = "WEEKLY"
        elif re.search(r"\b(every month|monthly)\b", t):
            recurrence = "MONTHLY"

        if re.search(r"\bremind", t):
            kind = "REMINDER"
        elif due and (re.search(rf"\b({_EVENT_WORDS})\b", t) or re.search(r"\b(calendar|event)\b", t)):
            kind = "EVENT"
        else:
            kind = "TASK"

        title = original.strip()
        for pattern in _STRIP:
            title = re.sub(pattern, " ", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title).strip(" ,.-:;")
        title = re.sub(r"^(to|that)\s+", "", title, flags=re.IGNORECASE) or original.strip()
        title = title[:1].upper() + title[1:]

        return self._call(
            "create_task", title=title, kind=kind, priority=priority, estimate_min=estimate, recurrence=recurrence,
            due_at=due.astimezone(tz).isoformat() if due else None,
        )
