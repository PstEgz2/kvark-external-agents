"""The KVARK agent-gateway client.

One class, one method per published endpoint, and one error type. The error carries the
machine-readable ``reason`` rather than the message, because the guide is explicit that
the messages are written for humans and will be reworded — everything in this application
that branches on a refusal branches on ``reason``.

Every call is recorded in a shared ring buffer before it returns, so the console can show
what actually went over the wire. That log is half the point of this application: a partner
integrating against the gateway needs to see the status and the reason, not a rendered
answer with the interesting part swallowed.
"""

from __future__ import annotations

import collections
import datetime
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class GatewayError(Exception):
    """A refusal, in the shape the gateway documents: ``{detail, reason}``."""

    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(f"{status} {reason}: {detail}")
        self.status = status
        self.reason = reason
        self.detail = detail

    #: Refusals where trying again cannot help. Two of these are the ones integrations get
    #: wrong: `agent_pending_approval` waits on a human, and `registration_capacity` is a
    #: queue a human drains. Polling either only adds the load that filled it.
    TERMINAL = frozenset(
        {
            "agent_pending_approval",
            "agent_disabled",
            "registration_capacity",
            "registration_conflict",
            "not_licensed",
            "user_not_permitted",
            "user_feature_missing",
            "agent_not_granted",
            "tool_not_callable",
        }
    )

    @property
    def retryable(self) -> bool:
        return self.reason not in self.TERMINAL


@dataclass
class CallRecord:
    """One request, as the console shows it."""

    at: datetime.datetime
    method: str
    path: str
    status: int | None
    reason: str | None
    ms: int
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


@dataclass
class CallLog:
    """The last few hundred calls. In memory on purpose — a debugging aid, not a record."""

    entries: collections.deque = field(default_factory=lambda: collections.deque(maxlen=300))

    def add(self, record: CallRecord) -> None:
        self.entries.appendleft(record)

    def recent(self, limit: int = 40) -> list[CallRecord]:
        return list(self.entries)[:limit]

    def clear(self) -> None:
        self.entries.clear()


call_log = CallLog()


def _refusal(response: httpx.Response) -> GatewayError:
    """Read a refusal, tolerating a body that is not the documented shape.

    A 502 from something in front of the gateway, or an unhandled 500, carries no
    ``reason`` — and an integration that assumes it does turns someone else's outage into
    a KeyError in its own logs.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    reason = body.get("reason") or f"http_{response.status_code}"
    detail = body.get("detail")
    if not isinstance(detail, str):
        detail = response.text[:500] or response.reason_phrase
    return GatewayError(status=response.status_code, reason=str(reason), detail=detail)


class Gateway:
    """A thin, complete client for ``/agent-api/v1``.

    Two credentials, exactly as the guide describes them: ``api_key`` says which agent this
    is and is held for the lifetime of the process; ``user_token`` says who it is acting
    for and is passed per call, because one running agent serves several signed-in people.
    """

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    # -- plumbing ---------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        user_token: str | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        note: str = "",
        expect: tuple[int, ...] = (200, 201, 202),
    ) -> Any:
        headers: dict[str, str] = {}
        if api_key:
            headers["X-Agent-Key"] = api_key
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"

        started = time.perf_counter()
        status: int | None = None
        reason: str | None = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, f"{self.base_url}{path}", headers=headers, json=json, params=params
                )
            status = response.status_code
            if status not in expect:
                error = _refusal(response)
                reason = error.reason
                raise error
            return response.json() if response.content else None
        except httpx.HTTPError as transport:
            # A gateway that is not answering at all is a different problem from one that is
            # refusing, and the console has to be able to tell them apart.
            reason = "transport_error"
            raise GatewayError(0, "transport_error", str(transport)) from transport
        finally:
            call_log.add(
                CallRecord(
                    at=datetime.datetime.now(datetime.UTC),
                    method=method,
                    path=path,
                    status=status,
                    reason=reason,
                    ms=int((time.perf_counter() - started) * 1000),
                    note=note,
                )
            )

    # -- 1. registration --------------------------------------------------------

    async def register(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Create the registration and receive the key. No credentials — this is where they come from.

        The response is the only time ``api_key`` is readable, so the caller persists it
        before doing anything else.
        """
        return await self._call("POST", "/register", json=manifest, note="registration", expect=(201,))

    # -- 5. manifest updates ----------------------------------------------------

    async def submit_manifest(self, api_key: str, manifest: dict[str, Any], changelog: str | None) -> dict[str, Any]:
        """Tell KVARK what changed. Agent key only — the agent speaking about itself.

        Only the fields that changed: the submission is applied over the manifest in force,
        so an unmentioned field keeps its value and a null clears it.
        """
        return await self._call(
            "POST",
            "/manifest",
            api_key=api_key,
            json={"manifest": manifest, "changelog": changelog},
            note="manifest update",
            expect=(202,),
        )

    # -- 3. chat ----------------------------------------------------------------

    async def start_turn(
        self,
        api_key: str,
        user_token: str,
        message: str,
        *,
        session_id: int | None = None,
        context_board_id: int | None = None,
        selected_document_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if session_id is not None:
            body["session_id"] = session_id
        if context_board_id is not None:
            body["context_board_id"] = context_board_id
        if selected_document_ids:
            body["selected_document_ids"] = selected_document_ids
        return await self._call(
            "POST", "/chat/turns", api_key=api_key, user_token=user_token, json=body, note="ask", expect=(202,)
        )

    async def get_turn(self, api_key: str, user_token: str, turn_id: int) -> dict[str, Any]:
        return await self._call(
            "GET", f"/chat/turns/{turn_id}", api_key=api_key, user_token=user_token, note="poll", expect=(200,)
        )

    # -- 4. the rest ------------------------------------------------------------

    async def search(
        self, api_key: str, user_token: str, q: str, *, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": q, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._call(
            "GET", "/search", api_key=api_key, user_token=user_token, params=params, note=q or "(browse by recency)"
        )

    async def preview(self, api_key: str, user_token: str, document_id: int) -> dict[str, Any]:
        return await self._call(
            "POST",
            "/preview",
            api_key=api_key,
            user_token=user_token,
            json={"document_id": document_id},
            note=f"document {document_id}",
        )

    async def preview_messages(
        self,
        api_key: str,
        user_token: str,
        document_id: int,
        *,
        after_page: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"document_id": document_id}
        if after_page is not None:
            body["after_page"] = after_page
        if limit is not None:
            body["limit"] = limit
        return await self._call(
            "POST",
            "/preview/messages",
            api_key=api_key,
            user_token=user_token,
            json=body,
            note=f"conversation {document_id}",
        )

    async def preview_page(self, api_key: str, user_token: str, document_id: int, page_number: int) -> dict[str, Any]:
        return await self._call(
            "POST",
            "/preview/page",
            api_key=api_key,
            user_token=user_token,
            json={"document_id": document_id, "page_number": page_number},
            note=f"page {page_number} of document {document_id}",
        )

    async def boards(self, api_key: str, user_token: str) -> list[dict[str, Any]]:
        return await self._call("GET", "/context-boards", api_key=api_key, user_token=user_token, note="boards")

    async def board_documents(self, api_key: str, user_token: str, board_id: int) -> dict[str, Any]:
        return await self._call(
            "GET",
            f"/context-boards/{board_id}/documents",
            api_key=api_key,
            user_token=user_token,
            note=f"board {board_id}",
        )

    async def tools(self, api_key: str, user_token: str) -> list[dict[str, Any]]:
        return await self._call("GET", "/tools", api_key=api_key, user_token=user_token, note="tool list")

    async def call_tool(
        self, api_key: str, user_token: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"/tools/{tool_name}",
            api_key=api_key,
            user_token=user_token,
            json=arguments,
            note=tool_name,
        )
