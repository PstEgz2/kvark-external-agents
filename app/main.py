"""The console.

A partner application that exercises every endpoint KVARK's agent gateway publishes, so
the gateway can be driven end to end by hand: register, wait for approval, sign a person
in, ask, search, read, browse boards, call tools, and submit a new manifest.

It is deliberately a *thin* client. Nothing here interprets an answer or retries around a
refusal — every call goes out as the guide describes it and whatever comes back is shown,
including the status and the machine-readable reason. An integration bug and a gateway bug
look different on this screen, which is the entire reason it exists.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import secrets
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.identity import Identity, IdentityError, sign_in, token_expiry
from app.kvark import Gateway, GatewayError, call_log
from app.store import AgentIdentity, Store

app = FastAPI(title="KVARK External Agent", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

gateway = Gateway(settings.gateway_base)
store = Store(settings.state_path)

#: Signed-in people, keyed by an opaque cookie. Held in memory rather than in the cookie
#: itself: the value is a KVARK access token, and a token in a browser cookie is a token in
#: every log and every extension between here and the browser.
_sessions: dict[str, Identity] = {}

SESSION_COOKIE = "agent_session"

#: Tools the gateway can expose. Shown as ready-made argument templates so a tool can be
#: called without reading KVARK's source — the live list still comes from `GET /tools`,
#: which returns only what this agent was actually granted.
TOOL_TEMPLATES: dict[str, dict[str, Any]] = {
    "search_knowledge_base": {"query": "water use"},
    "search_within_document": {"document_id": 1, "query": "water use"},
    "get_page": {"document_id": 1, "page_number": 1},
    "get_document_outline": {"document_id": 1},
    "list_document_versions": {"document_id": 1},
    "filter_documents": {"document_types": [], "keywords": []},
    "firecrawl_web_search": {"query": "environmental reporting standards"},
}


# ---------------------------------------------------------------------------
# session helpers
# ---------------------------------------------------------------------------


def _identity(request: Request) -> Identity | None:
    key = request.cookies.get(SESSION_COOKIE)
    return _sessions.get(key) if key else None


def _page(request: Request, template: str, **context: Any) -> HTMLResponse:
    """Render a console page with the header every page shares."""
    agent = store.load()
    return templates.TemplateResponse(
        request,
        template,
        {
            "agent": agent,
            "identity": _identity(request),
            "settings": settings,
            "calls": call_log.recent(),
            "tool_templates": TOOL_TEMPLATES,
            **context,
        },
    )


def _refusal_context(error: GatewayError) -> dict[str, Any]:
    """A refusal in the form the templates render.

    ``reason`` first, because that is what an integration branches on, and the guidance
    that goes with it — the two refusals that are not worth retrying are the two people
    retry hardest.
    """
    guidance = {
        "agent_pending_approval": "Not transient. An administrator has to approve this agent; polling will not help.",
        "registration_capacity": "The approval queue is full. Back off substantially — this is not a rate limit.",
        "registration_conflict": "That name is taken. The slug is permanent, so pick a different name.",
        "not_licensed": "This deployment's certificate does not include external agents.",
        "user_not_permitted": (
            "The person is signed in but has not been granted this agent. An administrator adds "
            "feature-external-agent-<slug> to their role on the roles page."
        ),
        "user_feature_missing": "The person themselves lacks the feature this call needs.",
        "agent_not_granted": "The agent was approved without this capability. An administrator grants it.",
        "identity_rejected": "The bearer token was not accepted. Sign in again.",
        "no_identity": "This call needs a user token as well as the agent key.",
        "unknown_agent_key": "KVARK does not recognise this key. It may have been uninstalled.",
        "agent_disabled": "An administrator switched this agent off. Approving it again turns it back on.",
        "tool_not_callable": "No such externally-callable tool — or it exists and is internal. The answer is the same.",
        "transport_error": "The gateway did not answer at all. Check that it is running and reachable.",
    }
    return {
        "error": {
            "status": error.status,
            "reason": error.reason,
            "detail": error.detail,
            "guidance": guidance.get(error.reason, ""),
            "retryable": error.retryable,
        }
    }


# ---------------------------------------------------------------------------
# health — section 8 of the guide
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Answered from memory, as the guide asks.

    Nothing downstream is checked: being unable to reach KVARK does not mean this agent
    cannot serve requests, and a health endpoint that fails when its dependency does turns
    one outage into two.
    """
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# registration and manifest — sections 1 and 5
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    agent = store.load()
    if agent is None:
        return RedirectResponse("/setup", status_code=303)
    if _identity(request) is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/chat", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request) -> HTMLResponse:
    return _page(request, "setup.html")


@app.post("/setup", response_class=HTMLResponse)
async def do_register(
    request: Request,
    name: str = Form(...),
    version: str = Form(""),
    description: str = Form(""),
    publisher: str = Form(""),
    contact: str = Form(""),
    base_url: str = Form(""),
    health_url: str = Form(""),
    turn_timeout_seconds: str = Form(""),
    requested_features: str = Form(""),
    requested_tools: str = Form(""),
) -> HTMLResponse:
    """Register, and persist the key before doing anything else with it."""
    manifest: dict[str, Any] = {"name": name.strip()}
    for field_name, value in (
        ("version", version),
        ("description", description),
        ("publisher", publisher),
        ("contact", contact),
        ("base_url", base_url),
        ("health_url", health_url),
    ):
        if value.strip():
            manifest[field_name] = value.strip()
    if turn_timeout_seconds.strip():
        try:
            manifest["turn_timeout_seconds"] = int(turn_timeout_seconds)
        except ValueError:
            return _page(request, "setup.html", form_error="turn_timeout_seconds must be a whole number of seconds.")
    manifest["requested_features"] = [item.strip() for item in requested_features.split(",") if item.strip()]
    manifest["requested_tools"] = [item.strip() for item in requested_tools.split(",") if item.strip()]

    try:
        receipt = await gateway.register(manifest)
    except GatewayError as refusal:
        return _page(request, "setup.html", submitted=manifest, **_refusal_context(refusal))

    store.save(
        AgentIdentity(
            agent_id=receipt["agent_id"],
            slug=receipt["slug"],
            api_key=receipt["api_key"],
            key_prefix=receipt["key_prefix"],
            status_at_registration=receipt["status"],
            manifest=manifest,
        )
    )
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/forget")
async def forget(request: Request) -> RedirectResponse:
    """Drop our copy of the registration. The agent row stays in KVARK."""
    store.clear()
    return RedirectResponse("/setup", status_code=303)


@app.get("/manifest", response_class=HTMLResponse)
async def manifest_page(request: Request) -> HTMLResponse:
    return _page(request, "manifest.html")


@app.post("/manifest", response_class=HTMLResponse)
async def submit_manifest(
    request: Request,
    manifest_json: str = Form(...),
    changelog: str = Form(""),
) -> HTMLResponse:
    agent = store.load()
    if agent is None:
        return RedirectResponse("/setup", status_code=303)
    try:
        submitted = json.loads(manifest_json or "{}")
    except ValueError as bad_json:
        return _page(request, "manifest.html", form_error=f"That is not valid JSON: {bad_json}")
    if not isinstance(submitted, dict):
        return _page(request, "manifest.html", form_error="A manifest submission is a JSON object.")

    try:
        version = await gateway.submit_manifest(agent.api_key, submitted, changelog.strip() or None)
    except GatewayError as refusal:
        return _page(request, "manifest.html", draft=manifest_json, **_refusal_context(refusal))

    # Recorded locally as *submitted*, not applied: until an administrator accepts it this
    # agent still runs its approved manifest, and a local copy that pretended otherwise
    # would make the next partial update diff against something that was never in force.
    agent.submissions.append(
        {
            "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "submitted": submitted,
            "changelog": changelog.strip() or None,
            "manifest_version_id": version.get("id"),
            "status": version.get("status"),
        }
    )
    store.save(agent)
    return _page(request, "manifest.html", accepted=version)


# ---------------------------------------------------------------------------
# acting for a person — section 2
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _page(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
async def do_login(
    request: Request,
    identifier: str = Form(""),
    password: str = Form(""),
    token: str = Form(""),
) -> HTMLResponse:
    """Sign a person in, either with KVARK credentials or with a token they already hold.

    The token box is not decoration: it is how the `identity_rejected` path gets tested,
    and it is the shape the Microsoft-AD exchange will take when it lands — a token this
    application receives rather than mints.
    """
    if token.strip():
        who = Identity(label="(pasted token)", token=token.strip(), expires_at=token_expiry(token.strip()))
    else:
        try:
            who = await sign_in(settings.kvark_api_base, identifier.strip(), password)
        except IdentityError as refused:
            return _page(request, "login.html", form_error=str(refused), identifier=identifier)

    key = secrets.token_urlsafe(24)
    _sessions[key] = who
    response = RedirectResponse("/chat", status_code=303)
    response.set_cookie(SESSION_COOKIE, key, httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    key = request.cookies.get(SESSION_COOKIE)
    if key:
        _sessions.pop(key, None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _credentials(request: Request) -> tuple[AgentIdentity, Identity] | None:
    """Both credentials, or nothing. Almost every call needs the pair."""
    agent = store.load()
    who = _identity(request)
    return (agent, who) if agent and who else None


# ---------------------------------------------------------------------------
# chat — section 3
# ---------------------------------------------------------------------------


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    if _credentials(request) is None:
        return RedirectResponse("/login" if store.load() else "/setup", status_code=303)
    return _page(request, "chat.html")


@app.post("/api/chat/ask")
async def api_ask(request: Request) -> JSONResponse:
    """Start a turn. Returns the handle; the browser polls the endpoint below."""
    pair = _credentials(request)
    if pair is None:
        return JSONResponse({"error": {"reason": "not_signed_in"}}, status_code=401)
    agent, who = pair
    body = await request.json()

    documents = body.get("selected_document_ids") or None
    if isinstance(documents, str):
        documents = [int(part) for part in documents.replace(",", " ").split() if part.strip().isdigit()] or None

    try:
        accepted = await gateway.start_turn(
            agent.api_key,
            who.token,
            body["message"],
            session_id=body.get("session_id"),
            context_board_id=body.get("context_board_id"),
            selected_document_ids=documents,
        )
    except GatewayError as refusal:
        return JSONResponse(_refusal_context(refusal), status_code=200)
    return JSONResponse(accepted)


@app.get("/api/chat/turn/{turn_id}")
async def api_turn(request: Request, turn_id: int) -> JSONResponse:
    pair = _credentials(request)
    if pair is None:
        return JSONResponse({"error": {"reason": "not_signed_in"}}, status_code=401)
    agent, who = pair
    try:
        return JSONResponse(await gateway.get_turn(agent.api_key, who.token, turn_id))
    except GatewayError as refusal:
        return JSONResponse(_refusal_context(refusal), status_code=200)


# ---------------------------------------------------------------------------
# search, documents, boards, tools — section 4
# ---------------------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = "", limit: int = 20, cursor: str = "") -> HTMLResponse:
    pair = _credentials(request)
    if pair is None:
        return RedirectResponse("/login" if store.load() else "/setup", status_code=303)
    agent, who = pair
    if "q" not in request.query_params and not cursor:
        return _page(request, "search.html", q=q, limit=limit)
    try:
        results = await gateway.search(agent.api_key, who.token, q, limit=limit, cursor=cursor or None)
    except GatewayError as refusal:
        return _page(request, "search.html", q=q, limit=limit, **_refusal_context(refusal))
    return _page(request, "search.html", q=q, limit=limit, results=results)


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request, document_id: int | None = None, mode: str = "pages") -> HTMLResponse:
    pair = _credentials(request)
    if pair is None:
        return RedirectResponse("/login" if store.load() else "/setup", status_code=303)
    agent, who = pair
    if document_id is None:
        return _page(request, "documents.html", mode=mode)
    try:
        if mode == "messages":
            document = await gateway.preview_messages(agent.api_key, who.token, document_id)
        else:
            document = await gateway.preview(agent.api_key, who.token, document_id)
    except GatewayError as refusal:
        return _page(request, "documents.html", document_id=document_id, mode=mode, **_refusal_context(refusal))
    return _page(request, "documents.html", document_id=document_id, mode=mode, document=document)


@app.post("/documents/page", response_class=HTMLResponse)
async def document_page_image(
    request: Request, document_id: int = Form(...), page_number: int = Form(...)
) -> HTMLResponse:
    pair = _credentials(request)
    if pair is None:
        return RedirectResponse("/login", status_code=303)
    agent, who = pair
    try:
        image = await gateway.preview_page(agent.api_key, who.token, document_id, page_number)
    except GatewayError as refusal:
        return _page(request, "documents.html", document_id=document_id, mode="pages", **_refusal_context(refusal))
    return _page(request, "documents.html", document_id=document_id, mode="pages", image=image)


@app.get("/boards", response_class=HTMLResponse)
async def boards_page(request: Request, board_id: int | None = None) -> HTMLResponse:
    pair = _credentials(request)
    if pair is None:
        return RedirectResponse("/login" if store.load() else "/setup", status_code=303)
    agent, who = pair
    try:
        boards = await gateway.boards(agent.api_key, who.token)
    except GatewayError as refusal:
        return _page(request, "boards.html", **_refusal_context(refusal))
    scope = None
    if board_id is not None:
        try:
            scope = await gateway.board_documents(agent.api_key, who.token, board_id)
        except GatewayError as refusal:
            return _page(request, "boards.html", boards=boards, board_id=board_id, **_refusal_context(refusal))
    return _page(request, "boards.html", boards=boards, board_id=board_id, scope=scope)


@app.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request) -> HTMLResponse:
    pair = _credentials(request)
    if pair is None:
        return RedirectResponse("/login" if store.load() else "/setup", status_code=303)
    agent, who = pair
    try:
        granted = await gateway.tools(agent.api_key, who.token)
    except GatewayError as refusal:
        return _page(request, "tools.html", **_refusal_context(refusal))
    return _page(request, "tools.html", granted=granted)


@app.post("/tools", response_class=HTMLResponse)
async def run_tool(request: Request, tool_name: str = Form(...), arguments: str = Form("{}")) -> HTMLResponse:
    pair = _credentials(request)
    if pair is None:
        return RedirectResponse("/login", status_code=303)
    agent, who = pair
    try:
        parsed = json.loads(arguments or "{}")
    except ValueError as bad_json:
        return _page(request, "tools.html", form_error=f"Arguments must be a JSON object: {bad_json}")

    granted: list[dict[str, Any]] = []
    try:
        granted = await gateway.tools(agent.api_key, who.token)
        result = await gateway.call_tool(agent.api_key, who.token, tool_name, parsed)
    except GatewayError as refusal:
        return _page(request, "tools.html", granted=granted, tool_name=tool_name, **_refusal_context(refusal))
    return _page(request, "tools.html", granted=granted, tool_name=tool_name, result=result)


# ---------------------------------------------------------------------------
# the call log
# ---------------------------------------------------------------------------


@app.get("/calls", response_class=HTMLResponse)
async def calls_page(request: Request) -> HTMLResponse:
    return _page(request, "calls.html", all_calls=call_log.recent(300))


@app.post("/calls/clear")
async def clear_calls() -> RedirectResponse:
    call_log.clear()
    return RedirectResponse("/calls", status_code=303)


# ---------------------------------------------------------------------------
# a scripted walkthrough of the whole surface
# ---------------------------------------------------------------------------


@app.post("/api/smoke")
async def smoke(request: Request) -> JSONResponse:
    """Call every endpoint once and report what each answered.

    Not a test suite — it asserts nothing. It is the fastest way to see which parts of the
    surface this agent has actually been granted, which is the question being asked most
    often while an integration is being set up.
    """
    pair = _credentials(request)
    if pair is None:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    agent, who = pair
    steps: list[dict[str, Any]] = []

    async def step(label: str, coroutine: Any) -> Any:
        try:
            value = await coroutine
        except GatewayError as refusal:
            steps.append({"step": label, "ok": False, "status": refusal.status, "reason": refusal.reason})
            return None
        steps.append({"step": label, "ok": True})
        return value

    results = await step("search", gateway.search(agent.api_key, who.token, "", limit=3))
    document_id = None
    if isinstance(results, dict):
        items = results.get("results") or results.get("items") or []
        if items and isinstance(items[0], dict):
            document_id = items[0].get("document_id") or items[0].get("id")

    if document_id:
        await step("preview", gateway.preview(agent.api_key, who.token, int(document_id)))
        await step("preview/page", gateway.preview_page(agent.api_key, who.token, int(document_id), 1))
    else:
        steps.append({"step": "preview", "ok": False, "reason": "no readable document to try"})

    boards = await step("context-boards", gateway.boards(agent.api_key, who.token))
    if isinstance(boards, list) and boards:
        await step("context-boards/documents", gateway.board_documents(agent.api_key, who.token, boards[0]["id"]))

    granted = await step("tools", gateway.tools(agent.api_key, who.token))
    if isinstance(granted, list) and granted:
        first = granted[0]["name"]
        await step(
            f"tools/{first}", gateway.call_tool(agent.api_key, who.token, first, TOOL_TEMPLATES.get(first, {}))
        )

    accepted = await step("chat/turns", gateway.start_turn(agent.api_key, who.token, "Say hello in one sentence."))
    if isinstance(accepted, dict):
        # One poll only. The point is that polling answers, not that the turn finished —
        # a turn can take minutes and this endpoint must not hold the request open for them.
        await asyncio.sleep(settings.poll_seconds)
        await step("chat/turns/{id}", gateway.get_turn(agent.api_key, who.token, accepted["turn_id"]))

    return JSONResponse({"steps": steps})
