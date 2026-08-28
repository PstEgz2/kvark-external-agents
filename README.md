# kvark-external-agents

How to build an application that talks to KVARK's agent gateway — the written guide, and a
worked example you can run.

- **[docs/writing-an-external-agent.md](docs/writing-an-external-agent.md)** — the guide.
  Registration, acting for a person, asking questions, the refusals and what each one means.
  Verified against a running gateway rather than written from the source.
- **[docs/api-reference.md](docs/api-reference.md)** — every endpoint, its parameters and
  what it returns. The lookup, where the guide is the explanation.
- **This repository** — that guide as working code: a console that exercises every endpoint
  the gateway publishes, so the whole surface can be driven by hand.

The console is deliberately a *thin* client. Nothing here interprets an answer or retries
around a refusal. Every call goes out as the guide describes it, and whatever comes back is
shown — including the status and the machine-readable `reason`. That is the point: an
integration bug and a gateway bug look different on this screen.

It runs completely separately from KVARK: its own repository, its own container, its own
port. The only thing it knows about KVARK is where to reach it.

Start with `app/kvark.py` if you are writing your own client — it is one method per
published endpoint and one error type, and it is the part worth copying.

---

## What it covers

| Guide section | Here |
|---|---|
| 1 · Register | **Agent** tab. Persists the key, which is shown once. |
| 2 · Act for a person | **Sign in**. KVARK credentials, a pasted bearer token, or a token posted here by KVARK's own *Open agent, signed in*. |
| 3 · Ask a question | **Chat**. `202` handle, then polls until `done` or `failed`. Session continuation, board scoping, document scoping. |
| 4 · The other things | **Search**, **Documents** (pages, conversations, signed page images), **Boards**, **Tools**. |
| 5 · Changing what you are | **Manifest**. Partial submissions, the `submitted` vs `document` diff, the refusals worth provoking. |
| 6 · Errors | Every refusal renders as `status · reason · detail`, with a note on whether retrying can help. |
| 7 · A minimal agent | `app/kvark.py` is that snippet, finished. |
| 8 · Health | `GET /healthz`, answered from memory. |
| — | **Open KVARK** on every page, for the trip back. |

Plus a **Calls** tab holding every request this process has made, and a one-click smoke run
that calls the whole surface once and reports what each endpoint answered.

---

## Running it

Needs a KVARK stack with the gateway up. From the KVARK worktree:

```bash
docker compose -f docker-compose.windows.yml --profile gateway \
  up -d postgres nats minio backend agent-gateway scheduler-worker chat-worker
```

Gateway on `:8010`, backend on `:8008`. A `license.lic` must sit in the worktree root — the
gateway fails closed without one and registration answers `403 not_licensed`.

Then here:

```bash
docker compose up -d --build
```

Open <http://localhost:8099>.

The container reaches KVARK through `host.docker.internal`, because KVARK's compose runs
with `network_mode: host`. Point it somewhere else with `KVARK_GATEWAY_BASE` and
`KVARK_API_BASE`.

### Without Docker

```bash
pip install -r requirements.txt
KVARK_GATEWAY_BASE=http://localhost:8010/agent-api/v1 \
KVARK_API_BASE=http://localhost:8008/api \
AGENT_STATE_PATH=./data/agent.json \
uvicorn app.main:app --port 8099
```

### Pointing it at a deployment

A deployment serves the gateway on the same origin as KVARK itself, proxied under
`/agent-api/`, so there is one host and one port to know:

```bash
KVARK_GATEWAY_BASE=https://kvark.example/agent-api/v1
KVARK_API_BASE=https://kvark.example/api
```

A PR preview serves a self-signed certificate that names neither its host nor its address, so
verification there fails on the chain *and* the hostname and cannot be fixed by trusting the
certificate. `KVARK_VERIFY_TLS=false` is how you talk to one. Leave it alone for anything
real.

### Settings

| | |
|---|---|
| `KVARK_GATEWAY_BASE` | The gateway. Every partner-facing call goes here. |
| `KVARK_API_BASE` | KVARK's own API, used for exactly one call — exchanging a person's credentials for a token. |
| `KVARK_WEB_URL` | KVARK's interface, for the **Open KVARK** link. Inferred from `KVARK_API_BASE` when unset, which is right for a deployment and wrong for a local stack that serves the two on different ports. |
| `KVARK_VERIFY_TLS` | Verify KVARK's certificate. On by default; any unrecognised value leaves it on. |
| `AGENT_NAME` | The slug derived from it is permanent. A rename means a new registration. |
| `AGENT_HEALTH_URL` | Leave empty unless a public HTTPS address fronts this container. |
| `AGENT_STATE_PATH` | Where the registration receipt lands. The API key is shown once; this is the only copy. |

---

## Moving between KVARK and here

Both directions work without signing in twice, and neither needs the two applications to
share anything but a browser.

**KVARK → here.** KVARK's **Agents** page offers *Open agent, signed in*, which posts the
person's KVARK token to this application's `/login`. That endpoint accepts a token it did not
mint — the same door the directory exchange will come through — so nothing here is specific
to that button. The token is only carried over https, or over http on loopback: a bearer
credential must not cross an unencrypted hop, and loopback never reaches a wire.

**Here → KVARK.** The **Open KVARK** link. Nothing is carried at all: somebody who arrived
from KVARK is still signed in there, on KVARK's own origin, and returning needs only an
address.

---

## The walkthrough

1. **Register** on the Agent tab. Store nothing yourself — the key lands in
   `AGENT_STATE_PATH` and is the only copy that will ever exist.
2. Every call now answers `403 agent_pending_approval`. That is correct and it is not
   transient.
3. **The umbrella permission is already granted.** A migration gives the `Administrator`
   role `feature-external-agents`, read and write, so a deployment built from migrations
   arrives with the surface switched on rather than invisible to everyone including the
   seeded `admin`. Read means *act through an agent*, write means *administer them* — the
   feature is deliberately not single-flag, so both are granted.

   On a deployment predating that migration, or to grant a different role:

   ```bash
   python scripts/kvark_admin.py bootstrap --role Administrator
   ```

4. **Approve** it, as an administrator would:

   ```bash
   python scripts/kvark_admin.py list
   python scripts/kvark_admin.py approve <id> \
     --features feature-chat,feature-search,feature-context-board \
     --tools search_knowledge_base,get_page,get_document_outline
   ```

5. **Put it on a role.** This is the step people miss. Approving creates the agent's catalog
   row but grants it to nobody, so calls answer `403 user_not_permitted` until:

   ```bash
   python scripts/kvark_admin.py permit <slug> --role Administrator
   ```

6. **Sign in** as someone in that role and drive the tabs.
7. **Submit a manifest update** and watch it stay pending — the agent goes on running what
   it was approved with. Then `accept`, or `reject`, or `accept` an older version to roll
   back.
8. **Disable** it and watch the next call answer `403 agent_disabled`.

Everything the script does is also on KVARK's Settings → Agents page. The script exists so
the walkthrough is repeatable, not because the page is missing anything.

---

## Two things that will not work locally

**Health checks.** `health_url` must be `https` and must resolve to a globally routable
address — checked at registration and again before every probe, because DNS is the
partner's to change. A container on your laptop is neither, so leave `AGENT_HEALTH_URL`
empty and the agent is simply never probed, which is not an error. To exercise the health
pill, put a public HTTPS tunnel in front of `:8099` and register that URL. Note the guard
does not follow redirects: a redirecting health endpoint reads as unhealthy.

**Anything needing a corpus.** Search, previews and grounded answers need documents the
signed-in person may read. An empty deployment answers correctly and emptily.

---

## Phase 2 — dropping the password exchange

This application signs people in by posting their KVARK username and password to
`/api/auth/login`. That is fine for a test harness and wrong for a real partner: an
application holding KVARK passwords is exactly what an external agent should not be.

The gateway already carries the better path. `IdpTokenAuthenticator` accepts a token issued
by the directory the accounts came from, once the deployment sets:

```
AGENT_GATEWAY_IDP_ISSUER=…
AGENT_GATEWAY_IDP_AUDIENCE=…
AGENT_GATEWAY_IDP_JWKS_URL=https://…
```

Nothing else in this application changes when that lands — everything downstream already
treats the token as opaque, and the **Sign in** tab's token box is already that shape.

KVARK's *Open agent, signed in* uses that same door today, and is an interim measure rather
than the destination: it hands over the person's full KVARK access token, which reaches every
internal endpoint they can, not only the dozen this gateway publishes. That is wider than the
gateway's own promise — an agent seeing no more than the person it acts for — and it is
accepted deliberately while the directory exchange is unconfigured. The exchange replaces it
with a token scoped to this gateway, and the button then carries that instead.

---

## Layout

```
app/kvark.py      the gateway client — one method per endpoint, one error type
app/identity.py   getting a token for the person being acted for
app/store.py      the registration receipt, written atomically
app/main.py       the console
app/templates/    one page per tab
scripts/          the administrator's side of the flow
```
