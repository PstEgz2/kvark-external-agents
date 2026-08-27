# Writing an external agent for KVARK

A guide for building a partner application that talks to KVARK's agent gateway.

Everything here is from the gateway on this branch. If a detail contradicts the running
service, the service is right — check `GET /agent-api/v1/docs` for the live schema.

---

## The shape of it

Your agent is an ordinary HTTP client. It holds one long-lived key, and it acts **on behalf of
a signed-in person** — never on its own authority. Two ideas follow from that, and most
integration mistakes come from missing one of them:

1. **Two credentials, not one.** Your key says *which agent this is*. A user token says *who
   it is acting for*. Almost every call needs both. The answers your agent gets are the
   answers that person is allowed to have, not the answers you are.
2. **Nothing you say takes effect until a human agrees.** Registration and manifest updates
   are *requests*. An administrator approves them. Your agent runs the manifest it was
   approved with until then.

```
                    X-Agent-Key: kvag_…          ← which agent
                    Authorization: Bearer …      ← which person
   your agent ───────────────────────────────►  KVARK gateway  :8010
                                                      │
                    202 / 200 / a refusal ◄───────────┘
```

Base URL for everything below: `http://<kvark-host>:8010/agent-api/v1`

---

## 1. Register

One call, no credentials — it is where credentials come from.

```http
POST /agent-api/v1/register
Content-Type: application/json

{
  "name": "Acme Procurement",
  "version": "1.0.0",
  "description": "Answers procurement questions from the supplier document set.",
  "publisher": "Acme Corp",
  "contact": "integrations@acme.example",
  "base_url": "https://agents.acme.example/procurement",
  "health_url": "https://agents.acme.example/procurement/healthz",
  "requested_features": ["feature-search", "feature-chat"],
  "requested_tools": ["search_knowledge_base", "get_page"],
  "turn_timeout_seconds": 120
}
```

```http
201 Created

{
  "agent_id": 17,
  "slug": "acme-procurement",
  "api_key": "kvag_dQw4w9WgXcQ...",
  "key_prefix": "kvag_dQw4w9W",
  "status": "pending"
}
```

**Store `api_key` immediately.** It is shown once and never again — KVARK keeps only a hash.
Lose it and you register afresh under a new name.

Things worth knowing:

- `name` becomes your permanent `slug`. Everything else about you can change; that cannot.
- `requested_features` and `requested_tools` are a **request**. An administrator decides what
  you actually get, and may give you less. Ask for what you need and handle being given less.
- `health_url` is the one address KVARK will call *out* to. It must be **https** and must
  resolve to a **public** address — no internal hosts, no loopback. Any 2xx means healthy.
  Omit it and your agent is simply never health-checked.
- `turn_timeout_seconds` (30–86400) is how long *you* are willing to wait on a turn you
  started. It only ever narrows KVARK's own answer, never extends it. Omit it unless your
  client genuinely gives up sooner.
- The whole manifest must be under **10,000 characters**.

Then you wait. Until an administrator approves you, every call returns:

```json
{ "detail": "This agent's registration is awaiting approval.", "reason": "agent_pending_approval" }
```

That is a `403`, and it is not a retryable error — polling will not make it approve faster.

One more thing about registering, because it bites hardest while you are still testing:
**there are two caps, and the tighter one counts your address.** A deployment allows a
limited number of registrations *awaiting a decision* in total, and a much smaller number
from any single origin — five, by default. Both count pending rows only, so approving or
rejecting what is already queued is what clears them.

Register a handful of throwaway agents from one machine and the sixth answers
`503 registration_capacity`, which reads like the deployment is full when in fact it is you.
The fix is to have the earlier ones approved or rejected, not to wait and not to retry.

---

## 2. Act for a person

Once approved, your key starts working. It was valid the whole time; approval is what makes
KVARK honour it.

Every call except registration and manifest updates needs **both** headers:

```http
X-Agent-Key: kvag_dQw4w9WgXcQ...
Authorization: Bearer <the user's token>
```

The bearer token is how you say *who you are acting for*. The deployment decides which kinds
it accepts — a KVARK-issued access token always works; some deployments also accept an
identity-provider token. If you send one it does not recognise, you get
`401 identity_rejected` — it will **not** silently fall back to another method.

The person must exist in KVARK, must be permitted to use your agent, and must themselves have
the feature you are using. Any of those failing is a `403` with a distinct `reason`.

**The two credentials are checked in order, and your key is checked first.** While you are
still awaiting approval every call answers `agent_pending_approval` — even one you sent with
no bearer token at all, or a plainly invalid one. So you cannot use this API to test your
token plumbing until you are approved: until then it never looks at the token, and a green
light on the agent half tells you nothing about the person half.

---

## 3. Ask a question

Turns are asynchronous. You get a handle, then you poll.

```http
POST /agent-api/v1/chat/turns
X-Agent-Key: …
Authorization: Bearer …

{ "message": "What did our 2025 assessment conclude about water use?" }
```

```http
202 Accepted
{ "turn_id": 4821, "session_id": 913, "scope_truncated": false }
```

Then poll until it settles:

```http
GET /agent-api/v1/chat/turns/4821
→ { "turn_id": 4821, "session_id": 913, "status": "running" }
→ { "turn_id": 4821, "session_id": 913, "status": "done", "answer": "…", "sources": […] }
```

- `status` is `running`, `done`, or `failed`. **Treat `failed` as terminal** — ask again
  rather than polling on. It means the turn has outlived every budget it had and is presumed
  dead; the turn is not *cancelled*, so in the rare case it was merely very slow a later poll
  may still say `done`. Do not build on that.
- `sources` lists what the answer drew on. Empty while running, and for an answer that cited
  nothing. Each entry names the document and the pages the answer used.
- **`answer` carries citation markup, and you have to deal with it.** The text is wrapped in
  markers naming the document and page each passage came from:

  ```
  [based_on document_id='4' slide_number='7']
  The policy states that the Board as a whole is assessed by …
  [/based_on]
  ```

  Shown to a person verbatim, that is what they read. There may be several such blocks in
  one answer, and a block may be unclosed — the marker text is generated, not assembled, so
  treat it as best-effort rather than as a grammar you can rely on.

  You have two sane options: strip the markers and use `sources` for citations, which is
  structured and already parsed; or parse the markers yourself when you want to attribute
  individual passages rather than the answer as a whole, which `sources` cannot express.
- Pass `session_id` back on the next turn to continue the conversation. You may only continue
  sessions **your agent** started; someone else's answers `404`.
- `message` may be up to 50,000 characters.
- A turn can take minutes. Poll at a sensible interval — a second or two, not a tight loop.

### Scoping a question

Optionally, `context_board_id` scopes the question to one board, and `selected_document_ids`
scopes it to specific documents. Three things about them are worth knowing before you use
either:

- **Scoping changes the kind of turn it is.** A scoped turn reads only what you named and
  cannot search the wider knowledge base at all. That is what makes "answered only from
  these" a guarantee rather than a preference — and it is also why a scope that resolves to
  nothing is refused rather than widened.
- **The two cannot be combined.** Sending both is a `422`. Silently preferring one would
  answer from a scope you did not ask for, and nothing in the response would say which.
  Sending `selected_document_ids: []` is also a `422`: an empty list is not "no scope", and
  treating it as one would quietly answer from the whole corpus.
- **A scope has a ceiling.** Every scoped document's content enters the prompt, so only the
  first 50 or so are read. When that happens `scope_truncated` comes back `true` on the
  `202` — the answer is drawn from part of what you asked for, and a caller that needs all
  of it must split the question. `GET /context-boards/{id}/documents` tells you in advance
  what a board-scoped question would actually read.

`selected_document_ids` are integers, not strings, and at most 500 per request. A malformed
id is refused rather than dropped — dropping it is the difference between an error and a
confidently wrong answer.

---

## 4. The other things you can do

All of these need both credentials, and all are gated on what the administrator granted you
**and** on what the person is allowed to see.

| What | Call |
|---|---|
| Search the corpus | `GET /search?q=…` |
| Read a document | `POST /preview` |
| Read a conversation document | `POST /preview/messages` |
| Get a signed URL for a page image | `POST /preview/page` |
| List the person's context boards | `GET /context-boards` |
| See what a board-scoped question would read | `GET /context-boards/{board_id}/documents` |
| List the tools you may call | `GET /tools` |
| Call one | `POST /tools/{tool_name}` |

`GET /tools` returns only tools *you* have been granted. A tool that exists but is internal,
and a tool that does not exist, both answer `404` — deliberately, so the status code cannot be
used to enumerate KVARK's internal tooling.

### The tools that can be granted

These are the externally callable ones. Everything else in KVARK's registry is internal and
is not reachable here, whatever you ask for.

| Tool | What it does |
|---|---|
| `search_knowledge_base` | Ranked search across the whole readable corpus. Where you start when you do not yet know which document holds the answer. |
| `search_within_document` | The same, narrowed to one document. |
| `get_page` | One page of one document, as text. |
| `get_document_outline` | A document's structure, for finding the right part before reading it. |
| `list_document_versions` | Other versions of a document, where search folded identical copies onto one result. |
| `filter_documents` | Narrow the corpus by document type, keyword, topic and the like. |
| `firecrawl_web_search` | Web search. Only where the deployment has configured it — without that it answers `503`, not an empty result. |

Ask for these by name in `requested_tools`. Names are **not checked at registration**: a
request for a tool that does not exist is recorded exactly as one that does, and simply
cannot be granted. So a typo here is silent until an administrator cannot find the checkbox.
`GET /tools` after approval is what tells you what you actually got, and its `description` is
the authoritative text — the summaries above are for orientation.

---

## 5. Changing what you are

When you ship a new version, tell KVARK. Do **not** expect it to notice on its own — it does
not fetch anything from you except the health check.

```http
POST /agent-api/v1/manifest
X-Agent-Key: kvag_dQw4w9WgXcQ...        ← agent key only; no user token

{
  "manifest": { "version": "2.0.0", "requested_tools": ["search_knowledge_base", "get_page", "get_document_outline"] },
  "changelog": "Adds outline reading so answers can cite the section a passage came from."
}
```

```http
202 Accepted
{ "id": 41, "status": "pending", "version": "2.0.0", … }
```

This is the one call where your agent speaks **about itself** rather than for a person, so it
carries no user token.

**202 means recorded, not applied.** You go on running your approved manifest, with the
capabilities you already had, until an administrator accepts the new one. A version that asks
for a new tool does not receive it by being sent.

Rules that will bite you if you skip them:

- **Send only what changed.** The submission is applied over the manifest in force. A field
  you do not mention keeps its value; a field you set to `null` is cleared.
- **Unknown fields are refused**, not ignored. `{"verison": "2.0.0"}` gets a `422`. This is
  deliberate — silently accepting a typo would tell you the update landed when it had not.
  Note the asymmetry: **registration does not do this.** `POST /register` with a misspelled
  field answers `201` and quietly drops it, so a typo you get away with at registration is
  refused the moment you send it as an update.
- **One submission awaits review at a time.** Sending another replaces it. That is you
  changing your mind, not a queue.
- Write a real `changelog`. It is the thing the human deciding actually reads.

The administrator can accept it, refuse it, or later roll back to an earlier version. Every
version you have ever submitted is kept.

**A refusal is silent from where you stand.** Nothing on this API tells you a submission was
rejected, and no reason is returned to you — your calls go on working exactly as before,
because you go on running the manifest you were approved with. The only way to notice is
that the version you asked for never becomes the one in force. If a submission matters,
agree out of band on how you will hear about it.

---

## 6. Errors

Almost every refusal has the same shape:

```json
{ "detail": "This agent has been disabled.", "reason": "agent_disabled" }
```

**Branch on `reason`, never on the message text.** The messages are written for humans and
will be reworded.

**The exception, and you must code for it: a body that fails schema validation does not
carry a `reason` at all.** It comes back as the framework's own validation error, where
`detail` is a *list of objects* rather than a string:

```json
{ "detail": [ { "type": "value_error", "loc": ["body", "manifest"],
               "msg": "Value error, A manifest has no field(s) named: verison" } ] }
```

Both shapes are `422`. So a client that does `response.json()["reason"]` raises a
`KeyError` on the first typo it sends, and one that prints `detail` gets `[object Object]`.
Read `reason` defensively, fall back to the status code, and do not assume `detail` is text:

```python
body = response.json() if response.content else {}
reason = body.get("reason") or f"http_{response.status_code}"
detail = body.get("detail")
if not isinstance(detail, str):
    detail = response.text[:500]
```

The same defence covers a `502` from something in front of the gateway and an unhandled
`500`, neither of which carries a `reason` either.

| Status | Meaning for you | Reasons |
|---|---|---|
| `401` | Authenticate differently and try again | `no_agent_key`, `unknown_agent_key`, `no_identity`, `identity_rejected` |
| `403` | You are who you say you are and the answer is still no | `agent_pending_approval`, `agent_disabled`, `agent_not_granted`, `user_not_provisioned`, `user_not_permitted`, `user_feature_missing`, `not_licensed` |
| `404` | Not found, or not yours to see | `tool_not_callable`, `board_not_found` |
| `409` | That name is taken; pick another | `registration_conflict` |
| `422` | Understood and cannot be done as asked | `registration_rejected`, `empty_board_scope`, a malformed manifest |

`registration_rejected` is not only about registration, despite the name: a manifest
*update* that names an endpoint KVARK will not call, or a value outside its bounds, comes
back under the same reason.

| `503` | The approval queue is full. **Not** a rate limit; back off substantially, it clears when a human works through it | `registration_capacity` |

The three `403`s that look alike and are not:

- `agent_not_granted` — *you* were approved without this capability. An administrator ticks
  a box and it starts working. Nothing about the person changes it.
- `user_not_permitted` — the person is real and signed in, but has not been granted your
  agent. Per-agent access is a permission an administrator assigns on the roles page, and
  approving your agent does not assign it to anybody.
- `user_feature_missing` — the person does not have the underlying product feature. They
  would not be able to do this in KVARK itself either.

Telling them apart matters because only the first is about you. The other two are answered
by an administrator changing something about the person, and an integration that reports
them all as "not authorised" sends people to the wrong place.

Two that catch people out:

- `agent_pending_approval` is not transient. Waiting helps; retrying does not.
- `registration_capacity` is `503` rather than `429` precisely because the wait is not one you
  can compute. Do not retry in a loop — that is what filled the queue.

---

## 7. A minimal agent

```python
import time, httpx

BASE = "http://localhost:8010/agent-api/v1"

def register() -> str:
    r = httpx.post(f"{BASE}/register", json={
        "name": "Acme Procurement",
        "version": "1.0.0",
        "description": "Answers procurement questions.",
        "requested_features": ["feature-chat"],
    })
    r.raise_for_status()
    return r.json()["api_key"]          # shown once — persist it now

def ask(key: str, user_token: str, question: str) -> str:
    headers = {"X-Agent-Key": key, "Authorization": f"Bearer {user_token}"}

    started = httpx.post(f"{BASE}/chat/turns", json={"message": question}, headers=headers)
    if started.status_code != 202:
        raise RuntimeError(started.json()["reason"])   # branch on reason, not the message
    turn = started.json()["turn_id"]

    while True:
        state = httpx.get(f"{BASE}/chat/turns/{turn}", headers=headers).json()
        if state["status"] == "done":
            return state["answer"]
        if state["status"] == "failed":
            raise RuntimeError("the turn failed")      # terminal — ask again, do not poll on
        time.sleep(2)

def announce_new_version(key: str) -> None:
    httpx.post(f"{BASE}/manifest",
               json={"manifest": {"version": "2.0.0"}, "changelog": "Faster retrieval."},
               headers={"X-Agent-Key": key})            # 202: recorded, awaiting a human
```

---

## 8. Health endpoint

If you declared a `health_url`, serve it. KVARK polls it on an interval and shows the result
to administrators.

- Any **2xx** means healthy. Anything else, or a timeout, means not.
- It must be **https** and resolve to a **public** address. This is checked when you declare it
  and again before every call, because DNS is yours to change.
- **Redirects are not followed.** A redirecting health endpoint reads as unhealthy.
- Keep the response small and fast. Answer from memory; do not check your own dependencies in
  it unless being unable to reach them genuinely means you cannot serve requests.
- Nobody is paging on it, but an agent that reads as down is one an administrator may switch
  off.

---

## 9. Getting approved — what the administrator sees

Worth knowing, because it shapes what you should send:

- Your manifest, as you submitted it.
- Which capabilities you asked for, and which they are granting — as checkboxes they can
  change. Asking for something the deployment does not offer is not fatal; it just is not
  granted.
- On an update: **only the fields you actually changed**, plus your changelog. Not the whole
  document. So a submission of `{"version": "2.0.0"}` with a good changelog is easy to say yes
  to, and a submission that rewrites everything is not.
- Your health state and when it was last checked.
- The full history of every version you have submitted and what was decided.

They can disable you at any time. Your key stops working on the next call; the people you were
acting for are unaffected. Approving you again turns it back on.
