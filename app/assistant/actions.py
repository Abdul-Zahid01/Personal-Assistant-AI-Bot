"""Button taps ("✅ Done", "⏰ +30m", "✅ Approve plan"). Same result from Telegram and the web UI, and no LLM involved."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from app.db.models import Message
from app.domain.timeutil import fmt_dt, local_dt, parse_hhmm
from app.messaging import Reply
from app.services import format as fmt
from app.services.tasks import TaskError

if TYPE_CHECKING:  # pragma: no cover
    from app.container import Container

log = logging.getLogger(__name__)
STALE = Reply("That button is out of date.")


class ActionHandler:
    def __init__(self, container: "Container"):
        self.c = container

    async def run(self, data: str, channel: str = "web") -> Reply:
        parts = (data or "").split(":")
        async with self.c.chat_lock:
            async with self.c.session_factory() as session:
                ctx = await self.c.make_context(session)
                try:
                    reply = await self._dispatch(ctx, parts)
                    await session.commit()
                except (TaskError, ValueError) as exc:
                    await session.rollback()
                    reply = Reply(str(exc))
                except Exception:
                    await session.rollback()
                    log.exception("Action %s failed", data)
                    reply = Reply("Something went wrong on my side. Please try again.")
                session.add(Message(role="assistant", content=reply.text, channel=channel, created_at=self.c.now(),
                                    buttons=[b.as_pair() for b in reply.buttons] or None))
                await session.commit()
        self.c.after_change()
        return reply

    async def _dispatch(self, ctx, parts: list[str]) -> Reply:
        action = parts[0]
        if action in ("done", "snz", "tmr") and len(parts) >= 2:
            task = await ctx.tasks.get(parts[1])
            if task is None:
                return Reply("That task no longer exists.")
            if task.status == "DONE":
                return Reply(f"“{task.title}” is already done 👍")

            if action == "done":
                result = await ctx.tasks.complete(task, ctx.now)
                text = f"✅ Done: {task.title}."
                if result.next_task:
                    text += f" Next one: {fmt_dt(result.next_task.due_at, ctx.tz)}."
                return Reply(text)
            if action == "snz":
                minutes = int(parts[2]) if len(parts) > 2 else 30
                await ctx.tasks.snooze(task, ctx.now, minutes)
                return Reply(f"⏰ Snoozed “{task.title}” to {fmt_dt(task.due_at, ctx.tz)}.", fmt.task_buttons(task.id))
            # tomorrow: same local time, or the start of the working day if it had no time
            base = max(task.due_at, ctx.now) if task.due_at else ctx.now
            tomorrow = base.astimezone(ctx.tz).date() + timedelta(days=1)
            wall = base.astimezone(ctx.tz).timetz().replace(tzinfo=None) if task.due_at else parse_hhmm(ctx.prefs["work_start"])
            await ctx.tasks.update(task, ctx.now, {"due_at": local_dt(tomorrow, wall, ctx.tz)})
            return Reply(f"➡️ Moved “{task.title}” to {fmt_dt(task.due_at, ctx.tz)}.")

        if action == "plan" and len(parts) == 2:
            day = date.fromisoformat(parts[1])
            if day < ctx.now.astimezone(ctx.tz).date():
                return Reply("That day has already passed.")
            result = await ctx.planning.commit(day, ctx.now)
            return Reply(fmt.plan_text(result, ctx.tz, committed=True))
        return STALE
