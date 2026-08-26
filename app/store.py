"""Where the registration receipt is kept.

The API key is shown exactly once, at registration, so this file is the only copy that
will ever exist. Losing it means registering afresh under a new name — the slug is
permanent, so the old one cannot be reclaimed.

A JSON file rather than a database because that is the whole of this application's own
state: one agent, one key, and the manifest last submitted.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentIdentity:
    """What registration handed back, plus what has been sent since."""

    agent_id: int
    slug: str
    api_key: str
    key_prefix: str
    #: The status at the moment of registration. It is not refreshed — nothing on the
    #: gateway tells an agent its own status, and the console reads the live answer from
    #: whether calls are refused with `agent_pending_approval` instead.
    status_at_registration: str
    #: The manifest as this agent believes it stands. Kept so a partial update can be shown
    #: against something, and so the console can pre-fill the update form.
    manifest: dict[str, Any] = field(default_factory=dict)
    #: Every submission made since registration, newest last. Local record only — KVARK
    #: keeps the authoritative history and an administrator sees it there.
    submissions: list[dict[str, Any]] = field(default_factory=list)


class Store:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> AgentIdentity | None:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt state file must not take the console down: it renders as "not
            # registered", which is recoverable, rather than as a stack trace, which is not.
            return None
        try:
            return AgentIdentity(**raw)
        except TypeError:
            return None

    def save(self, identity: AgentIdentity) -> None:
        """Write atomically. A half-written file here loses the only copy of the key."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(asdict(identity), file, indent=2, ensure_ascii=False)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        """Forget the registration. The agent row stays in KVARK — this only drops our copy."""
        self.path.unlink(missing_ok=True)
