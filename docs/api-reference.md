# KVARK agent gateway — API reference

Every endpoint the gateway publishes, what it takes, and what it returns. This is the
lookup; **[writing-an-external-agent.md](writing-an-external-agent.md)** is the guide that
explains why the surface is shaped this way and in what order to use it.

The running gateway also serves its own schema at `/agent-api/v1/docs` and
`/agent-api/v1/openapi.json`. That is generated from the code and therefore cannot drift —
prefer it when the two disagree, and tell us, because this page is then wrong.

## Base URL

```
https://<kvark-host>/agent-api/v1
```

A deployment proxies the gateway under `/agent-api/` on KVARK's own origin, so there is one
host to know. A local stack runs it on its own port instead — `http://localhost:8010/agent-api/v1`
— because nothing sits in front of it there.

## Credentials

| Header | Says | Needed by |
|---|---|---|
| `X-Agent-Key: kvag_…` | which agent this is | everything except `POST /register` |
| `Authorization: Bearer …` | which person it acts for | everything except `POST /register`, `POST /manifest`, `GET /capabilities` |

The key is checked **before** the token. While a registration is pending, every call answers
`agent_pending_approval` — even one carrying no token at all — so a green light on the agent
half tells you nothing about the person half.

## Conventions

- `*` marks a required field.
- `?` marks a type that may be null.
- Every failure answers with `{"detail": "…", "reason": "…"}`. Match on `reason`, never on
  `detail`. The full list is in the guide's *Errors* section.

---

## Registration and identity

### `POST /register`

Create a registration and receive the API key for it. The only call needing no credentials —
it is where credentials come from.

**Body** — `AgentManifest`. Only `name` is required.

| Field | Type | |
|---|---|---|
| `name`* | string ≤120 | Display name. Its slug form identifies the agent for the rest of its life. |
| `version` | string? ≤50 | Your own version. Recorded at approval, so a later upgrade is visible. |
| `description` | string? ≤2000 | What this agent does, for the administrator deciding whether to approve it. |
| `publisher` | string? ≤200 | Who built it. |
| `contact` | string? ≤200 | How to reach whoever operates it. |
| `base_url` | string? ≤500 | Where the agent lives. Never called by KVARK; shown to administrators, and used by KVARK's *Open agent* link. |
| `health_url` | string? ≤500 | Where KVARK may ask whether you are up. Must be https and publicly resolvable. Omit it and you are never probed, which is not an error. |
| `turn_timeout_seconds` | integer? 30–86400 | How long you are willing to wait on a turn. Can only narrow KVARK's own budget. |
| `requested_features` | string[] ≤50 | A request, not a grant. |
| `requested_tools` | string[] ≤50 | Also only a request. |

**`201` → `RegistrationAccepted`**

| Field | Type | |
|---|---|---|
| `agent_id`* | integer | This agent's id in KVARK. |
| `slug`* | string | Stable identifier derived from the name. |
| `api_key`* | string | Send as `X-Agent-Key`. **Shown once and never again** — KVARK stores only a hash. |
| `key_prefix`* | string | The fragment an administrator sees, so both sides can name the same key. |
| `status`* | string | `pending` until approved. |

**Refusals** — `403 not_licensed` · `409 registration_conflict` (name taken) ·
`422 registration_rejected` (name cannot become an identifier) · `503 registration_capacity`
(approval queue full; retry later, and do not retry immediately — that is what filled it).

### `GET /capabilities`

What this deployment can grant, so you can ask for things that exist. Needs no user token —
it describes the deployment, not a person. Worth calling **before** you register.

**`200` → `Capabilities`**

| Field | Type | |
|---|---|---|
| `features`* | `{identifier}[]` | Put these in `requested_features`. |
| `tools`* | `{name, description}[]` | Put these in `requested_tools`. |

**Refusals** — `403 not_licensed`.

---

## Asking questions

### `POST /chat/turns`

Ask a question. Turns are asynchronous: you get a handle, then you poll.

**Body** — `TurnRequest`

| Field | Type | |
|---|---|---|
| `message`* | string | The question to ask. |
| `session_id` | integer? | Continue an existing conversation, so the turn has the earlier exchange as context. Only sessions this agent started. |
| `context_board_id` | integer? | Answer only from the documents on this board, resolved against what the signed-in user may read. |
| `selected_document_ids` | integer[]? | Restrict the answer to these documents. **This changes the kind of turn it is** — a scoped turn reads only what you named. |

**`202` → `TurnAccepted`**

| Field | Type | |
|---|---|---|
| `turn_id`* | integer | Poll this at `GET /chat/turns/{turn_id}`. |
| `session_id`* | integer | The conversation this turn belongs to. Pass it back to continue the thread. |
| `scope_truncated` | boolean | True when the requested scope held more documents than one turn may read and the rest were dropped. |

**Refusals** — `404` continuing a conversation that is not yours · `422` validation.

### `GET /chat/turns/{turn_id}`

Check on a turn. Poll every second or two; do not spin.

**Path** — `turn_id`* integer.

**`200` → `TurnStatus`**

| Field | Type | |
|---|---|---|
| `turn_id`*, `session_id`* | integer | |
| `status`* | string | `running` — poll again. `done` — `answer` is populated. `failed` — the turn outlived every budget. |
| `answer` | string? | The answer, once `status` is `done`. |
| `sources` | object[] | Documents the answer drew on. Empty while running, and for an answer that cited nothing. |

**Refusals** — `404` no such turn *for this agent and user*.

---

## Reading the corpus

Everything here is gated on what the administrator granted the agent **and** on what the
signed-in person may see. A document that person cannot read answers `404`, never `403` —
the two must look the same, or the status becomes a way to enumerate the corpus.

### `GET /search`

Search the knowledge base.

| Query param | Type | |
|---|---|---|
| `q` | string | Omit or pass empty to **browse** the readable corpus by recency instead of searching. |
| `limit` | integer | Results per page. |
| `cursor` | string? | Opaque, from the previous response's `next_cursor`. Bound to the query it was issued for. |

**`200` → `SearchResponse`**

| Field | Type | |
|---|---|---|
| `results` | `SearchResult[]` | Ranked results for this page. |
| `approx_total` | integer | **Bounded** count of ranked candidates, not an exact match count. |
| `next_cursor` | string? | Pass back as `cursor` for the next page. |
| `has_more` | boolean | Always consistent with `next_cursor`. |
| `query` | string | Echoed. Empty means the browse path. |

`SearchResult`: `document_id`*, `title`*, `snippet`*, `score`*, `relevance` (0.0 for browse),
`document_type`?, `page_count`?, `file_type`?, `file_size`?, `source`?, `languages[]`,
`is_message`, `summary`?, `source_url`?, `last_updated`?, `filename`?, `image_urls[]`.

**Refusals** — `400` cursor does not belong to this query · `401` identity ·
`403` agent, user or licence does not permit search.

### `POST /preview`

Read a document.

**Body** — `DocumentPreviewRequest`: `document_id`*, `after_page`? (pages after this one),
`limit`? (omit for the whole document).

**`200` → `DocumentPreviewResponse`**: `document_id`*, `total_pages`*, `title`?, `filename`?,
`file_type`?, `file_size`?, `source`?, `source_url`?, `document_type`?, `languages[]`,
`last_updated`?, `is_message`, and `content` as `{page_number, image_url?, text_content?}[]`.

When `is_message` is true, use `/preview/messages` instead.

### `POST /preview/messages`

Read a conversation document — Slack, Discord and the like, where pages are message windows.

**Body** — `DocumentPreviewRequest`, as above.

**`200` → `MessagePreviewResponse`**: `document_id`*, `total_pages`*, `title`?, `origin`?,
`pages` as `{page_number, messages[]}[]`, `has_more`.

### `POST /preview/page`

Get a signed URL for one page image.

**Body** — `DocumentPreviewPageRequest`: `document_id`*, `page_number`* (1-based).

**`200` → `PageImage`**: `image_url`* — **presigned, valid one hour**, so fetch promptly and
do not cache the response; `page_number`*.

---

## Context boards

### `GET /context-boards`

List the signed-in person's boards.

**`200` → `BoardSummary[]`**: `id`*, `name`*, `item_count`* — everything on the board,
*including* items this person cannot read and web links, so it will not always match what a
board-scoped question actually reads.

### `GET /context-boards/{board_id}/documents`

What a board-scoped question would actually read. Call this before scoping a turn if you want
to show a person what it will cover.

**Path** — `board_id`* integer.

**`200` → `BoardDocuments`**: `board_id`*, `documents`* as `{document_id, title?}[]`,
`withheld` (on the board but unreadable by this person — left out of scope), `web_links`
(a chat turn cannot read them), `truncated` (more readable documents than one question can
carry).

**Refusals** — `404` no such board, or this person cannot see it.

---

## Tools

### `GET /tools`

The tools **you** have been granted. Not the full registry.

**`200` → `ToolDescription[]`**: `name`*, `description`* — what the tool does, in the words
the chat agent is given.

### `POST /tools/{tool_name}`

Call one tool directly, without a chat turn.

**Path** — `tool_name`* string. **Body** — the tool's own arguments, an object whose shape
comes from the tool.

**`200` → `ToolCallResult`**

| Field | Type | |
|---|---|---|
| `tool`* | string | |
| `text`* | string | The tool's output, as the chat agent would receive it. |
| `sources` | object[] | Documents this call drew on, where the tool reports any. |
| `kind`* | string | `retriever` searches the corpus, `utility` inspects, `exploration` navigates a document. |

**Refusals** — `403 agent_not_granted` · `404 tool_not_callable`. A tool that exists but is
internal and a tool that does not exist **both** answer `404`, so the status cannot be used to
enumerate KVARK's internal tooling.

---

## Changing what you are

### `POST /manifest`

Submit a manifest update. Needs your key but no user token — it is the agent speaking about
itself.

Nothing takes effect until an administrator accepts it; you go on running the manifest you
were approved with.

**Body** — `ManifestSubmission`

| Field | Type | |
|---|---|---|
| `manifest` | object | **Only the fields that changed**, in the registration manifest's shape. Applied over what is in force. |
| `changelog` | string? | What changed and why, for the administrator. Shown beside the diff. |

Send only what changed. An administrator sees the diff, so `{"version": "2.0.0"}` with a good
changelog is easy to say yes to and a submission rewriting everything is not.

**`202` → `ManifestVersion`**: `id`*, `status`*, `version`*?, `changelog`*?, `submitted`*
(what you sent), `document`* (the result after applying it), `submitted_at`*,
`submitted_from_ip`*?, `reviewed_at`*?, `reviewed_by`*?.

**Refusals** — `422` the manifest does not fit the schema, or declares a `health_url` KVARK
will not call.

---

## What KVARK calls on you

Exactly one thing, and only if you asked for it: `GET` on the `health_url` you declared.
Everything else is you calling KVARK, so **an agent needs no inbound connectivity at all**.

- Any **2xx** is healthy; anything else, or a timeout, is not.
- Must be **https** and resolve to a **globally routable** address — checked at declaration
  and again before every probe, because DNS is yours to change.
- **Redirects are not followed.** A redirecting health endpoint reads as unhealthy.
- Answer from memory. Do not check your own dependencies unless being unable to reach them
  genuinely means you cannot serve requests.
