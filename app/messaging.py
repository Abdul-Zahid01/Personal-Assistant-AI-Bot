from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Button:
    label: str
    data: str  # action string understood by assistant.actions, e.g. "done:<task id>" (max 64 bytes for Telegram)

    def as_pair(self) -> list[str]:
        return [self.label, self.data]


@dataclass
class Reply:
    text: str
    buttons: list[Button] = field(default_factory=list)
