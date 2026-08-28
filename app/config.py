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


def _web_root(api_base: str) -> str:
    """KVARK's web address, inferred from its API address when not configured outright.

    A deployment serves the application and ``/api`` from one origin, so the inference holds
    there. A local stack does not — the API is on 8008 and the interface on the proxy's own
    port — which is why ``KVARK_WEB_URL`` exists and why the compose file sets it.
    """
    root = api_base.rstrip("/")
    return root[: -len("/api")] if root.endswith("/api") else root


#: Spelt out rather than compared against "true": an unrecognised value must leave
#: verification ON, so the safe state is the one that survives a typo.
_FALSE = frozenset({"0", "false", "no", "off"})


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


#: Defaults live here rather than inline in `from_env`, because an environment variable set
#: to the empty string must fall back to them — `os.environ.get` returns "" in that case, not
#: the default, and a blank description is a worse manifest than an unset one.
_DESCRIPTION = (
    "Operator console for the KVARK agent gateway. Exercises every published endpoint on "
    "behalf of a signed-in person and shows the raw status and refusal reason for each "
    "call. Reads only; it never writes back to KVARK."
)
_FEATURES = "feature-chat,feature-search,feature-context-board"
_TOOLS = (
    "search_knowledge_base,search_within_document,get_page,"
    "get_document_outline,list_document_versions,filter_documents"
)


@dataclass(frozen=True)
class Settings:
    #: The agent gateway. Every partner-facing call goes here.
    gateway_base: str
    #: Where KVARK's own web application lives, for the link back to it. A person who
    #: arrived here from KVARK is still signed in there — the browser kept that session on
    #: KVARK's own origin — so returning needs no token handed over, only an address.
    kvark_web_url: str
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
    agent_description: str
    agent_publisher: str
    agent_contact: str
    #: Where this agent answers. An administrator sees it on the agent's page in KVARK, so it
    #: should be an address that means something to a person — the landing page at `/`, not a
    #: port number. Nothing validates it and KVARK never calls it.
    base_url: str
    #: Declared to KVARK only when it is reachable from the public internet. KVARK refuses
    #: to probe a private address, so a local run leaves this empty and is never probed.
    health_url: str
    #: How long this agent is willing to wait on a turn it started. Omitted when zero, which
    #: is the right answer for most agents: it can only narrow KVARK's own budget.
    turn_timeout_seconds: int
    #: What to ask for. A request, not a grant — an administrator decides what is given.
    requested_features: tuple[str, ...]
    requested_tools: tuple[str, ...]
    #: How often the chat console asks KVARK whether a turn has settled.
    poll_seconds: float
    #: Whether to verify KVARK's TLS certificate. On by default and meant to stay on. A
    #: preview deployment serves a self-signed certificate that names neither its own host
    #: nor its address, so verification there fails on the chain *and* the hostname and
    #: cannot be fixed by trusting the certificate; turning this off is how you talk to one.
    verify_tls: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_base=_env("KVARK_GATEWAY_BASE", "http://localhost:8010/agent-api/v1").rstrip("/"),
            kvark_api_base=_env("KVARK_API_BASE", "http://localhost:8008/api").rstrip("/"),
            state_path=_env("AGENT_STATE_PATH", "/data/agent.json"),
            agent_name=_env("AGENT_NAME", "Egzakta Console"),
            agent_version=_env("AGENT_VERSION", "1.0.0"),
            agent_description=_env("AGENT_DESCRIPTION") or _DESCRIPTION,
            agent_publisher=_env("AGENT_PUBLISHER", "Egzakta"),
            agent_contact=_env("AGENT_CONTACT", "integrations@example.com"),
            base_url=_env("AGENT_BASE_URL", "http://localhost:8099").rstrip("/"),
            health_url=_env("AGENT_HEALTH_URL"),
            turn_timeout_seconds=int(_env("AGENT_TURN_TIMEOUT_SECONDS", "0") or 0),
            requested_features=_csv(_env("AGENT_REQUESTED_FEATURES") or _FEATURES),
            requested_tools=_csv(_env("AGENT_REQUESTED_TOOLS") or _TOOLS),
            poll_seconds=float(_env("AGENT_POLL_SECONDS", "2")),
            verify_tls=_env("KVARK_VERIFY_TLS", "true").lower() not in _FALSE,
            kvark_web_url=_env("KVARK_WEB_URL") or _web_root(_env("KVARK_API_BASE", "http://localhost:8008/api")),
        )


settings = Settings.from_env()


def declared_manifest() -> dict:
    """The manifest this agent would register with, built from its own configuration.

    Serving this rather than keeping a JSON file beside the code means the document an
    administrator reviews and the application they are reviewing cannot drift apart. Empty
    optional fields are omitted rather than sent as null, because null on an update *clears*
    a field and a registration should not assert absence it does not mean.
    """
    manifest: dict = {
        "name": settings.agent_name,
        "version": settings.agent_version,
        "description": settings.agent_description,
        "publisher": settings.agent_publisher,
        "contact": settings.agent_contact,
        "base_url": settings.base_url,
        "requested_features": list(settings.requested_features),
        "requested_tools": list(settings.requested_tools),
    }
    if settings.health_url:
        manifest["health_url"] = settings.health_url
    if settings.turn_timeout_seconds:
        manifest["turn_timeout_seconds"] = settings.turn_timeout_seconds
    return manifest
