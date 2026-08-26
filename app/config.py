"""Configuration for the external agent.

Everything here is about *this* application. Nothing KVARK owns is configured here except
the address of its two APIs, because an external agent is an ordinary HTTP client and the
only thing it needs to know about KVARK is where to reach it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    #: The agent gateway. Every partner-facing call goes here.
    gateway_base: str
    #: KVARK's internal API. Used for exactly one thing — exchanging a person's KVARK
    #: username and password for the access token this agent then acts with. A real
    #: partner integration would receive that token from an identity provider instead;
    #: see README "Phase 2".
    kvark_api_base: str
    #: Where the registration receipt is kept. The API key is shown once, so losing this
    #: file means registering afresh under a new name.
    state_path: str
    #: What this agent calls itself. Its slug form is permanent, so a rename means a new
    #: registration — the console says so before it lets you register.
    agent_name: str
    agent_version: str
    #: Declared to KVARK only when it is reachable from the public internet. KVARK refuses
    #: to probe a private address, so a local run leaves this empty and is never probed.
    health_url: str
    #: How often the chat console asks KVARK whether a turn has settled.
    poll_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_base=_env("KVARK_GATEWAY_BASE", "http://localhost:8010/agent-api/v1").rstrip("/"),
            kvark_api_base=_env("KVARK_API_BASE", "http://localhost:8008/api").rstrip("/"),
            state_path=_env("AGENT_STATE_PATH", "/data/agent.json"),
            agent_name=_env("AGENT_NAME", "Egzakta Console"),
            agent_version=_env("AGENT_VERSION", "1.0.0"),
            health_url=_env("AGENT_HEALTH_URL"),
            poll_seconds=float(_env("AGENT_POLL_SECONDS", "2")),
        )


settings = Settings.from_env()
