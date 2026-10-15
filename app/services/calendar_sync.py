"""Pushes time blocks to the calendar. Eventually consistent: failures are retried on the next run."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TimeBlock
from app.services.planning import CalendarPort

log = logging.getLogger(__name__)


async def reconcile(session: AsyncSession, calendar: CalendarPort | None, now: datetime) -> int:
    """Make the calendar match the database. Returns the number of calendar writes made.

    - PLANNED/DONE blocks with no event yet          -> create
    - PLANNED/DONE blocks changed since last sync    -> update
    - CANCELLED/SKIPPED blocks that have an event    -> delete the event
    """
    if calendar is None or not await calendar.is_connected():
        return 0

    dirty = (await session.execute(
        select(TimeBlock).where(
            or_(
                TimeBlock.status.in_(("CANCELLED", "SKIPPED")),
                TimeBlock.synced_at.is_(None),
                TimeBlock.updated_at > TimeBlock.synced_at,
            ),
            or_(TimeBlock.status.in_(("PLANNED", "DONE")), TimeBlock.calendar_event_id.is_not(None)),
        ).limit(100)
    )).scalars().all()

    writes = 0
    for block in dirty:
        block_id = block.id  # read now: a rollback expires the object and async sessions can't reload it
        try:
            if block.status in ("CANCELLED", "SKIPPED"):
                if block.calendar_event_id:
                    await calendar.delete_event(block.calendar_event_id)
                    writes += 1
                block.calendar_event_id = None
                block.synced_at = block.updated_at
            else:
                block.calendar_event_id = await calendar.upsert_event(
                    event_id=block.calendar_event_id, title=block.title, start=block.start_at, end=block.end_at,
                    block_id=block_id,
                )
                block.synced_at = block.updated_at
                writes += 1
            await session.commit()
        except Exception as exc:  # leave it dirty; retried next time
            await session.rollback()
            log.warning("Calendar sync failed for block %s: %s", block_id, exc)
            break  # most likely Google is unreachable; don't hammer it
    return writes
