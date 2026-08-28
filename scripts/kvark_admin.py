#!/usr/bin/env python
"""The administrator's side of the flow, as a script.

Everything here happens on KVARK's *internal* API — it is what a person with the agents
admin page open would be doing. It exists so the walkthrough is repeatable: approving an
agent, ticking its capabilities, and putting it on a role are three screens and about a
dozen clicks, and getting one of them wrong produces a refusal that looks like a bug in
the partner application.

The step people miss is the third. Approving an agent creates its own catalog row
(``feature-external-agent-<slug>``) but grants it to nobody. Until that identifier is on
somebody's role, every call that agent makes for them answers ``403 user_not_permitted``.

    python scripts/kvark_admin.py bootstrap --role Administrator      # once per deployment
    python scripts/kvark_admin.py list
    python scripts/kvark_admin.py show <agent_id>
    python scripts/kvark_admin.py approve <agent_id> --features feature-chat,feature-search
    python scripts/kvark_admin.py permit <agent_slug> --role Administrator
    python scripts/kvark_admin.py manifests <agent_id>
    python scripts/kvark_admin.py accept <agent_id> <manifest_id> --features … --tools …
    python scripts/kvark_admin.py reject <agent_id> <manifest_id> --reason "…"
    python scripts/kvark_admin.py disable <agent_id>
    python scripts/kvark_admin.py capabilities
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

# Windows consoles default to cp1252, which cannot encode the dashes and arrows in the
# messages below — and an encoding error mid-report loses the report, not just the dash.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

API = os.environ.get("KVARK_API_BASE", "http://localhost:8008/api").rstrip("/")
USER = os.environ.get("KVARK_ADMIN_USER", "admin")
PASSWORD = os.environ.get("KVARK_ADMIN_PASSWORD", "admin")


class AdminError(Exception):
    pass


def _verify_tls() -> bool:
    """Mirrors the agent's own KVARK_VERIFY_TLS, so both halves of the flow reach the same
    deployment. Any unrecognised value leaves verification on."""
    return os.environ.get("KVARK_VERIFY_TLS", "true").strip().lower() not in {"0", "false", "no", "off"}


def _token(client: httpx.Client) -> str:
    response = client.post(f"{API}/auth/login", json={"identifier": USER, "password": PASSWORD})
    if response.status_code != 200:
        raise AdminError(f"Sign-in failed ({response.status_code}): {response.text[:200]}")
    token = response.json().get("access_token")
    if not token:
        raise AdminError("Signed in but no access_token came back.")
    return token


def _call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, f"{API}{path}", **kwargs)
    if response.status_code >= 400:
        raise AdminError(f"{method} {path} → {response.status_code}: {response.text[:400]}")
    return response.json() if response.content else None


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------


def cmd_list(client: httpx.Client, args: argparse.Namespace) -> None:
    agents = _call(client, "GET", "/external-agents")
    if not agents:
        print("No agents registered.")
        return
    print(f"{'id':>4}  {'slug':<28} {'status':<10} {'approved':<10} health")
    for agent in agents:
        health = "—" if agent["last_health_ok"] is None else ("ok" if agent["last_health_ok"] else "down")
        print(
            f"{agent['id']:>4}  {agent['slug']:<28} {agent['status']:<10} "
            f"{str(agent['approved_version']):<10} {health}"
        )
        print(f"      features: {agent['feature_grants'] or '—'}  tools: {agent['tool_grants'] or '—'}")
        if agent["feature_identifier"]:
            print(f"      roles-page identifier: {agent['feature_identifier']}")


def cmd_show(client: httpx.Client, args: argparse.Namespace) -> None:
    _print(_call(client, "GET", f"/external-agents/{args.agent_id}"))


def cmd_capabilities(client: httpx.Client, args: argparse.Namespace) -> None:
    grantable = _call(client, "GET", "/external-agents/capabilities")
    print("features an agent can be granted:")
    for feature in grantable["features"]:
        print(f"  {feature['identifier']:<28} {feature['name']}")
    print("\ntools an agent can be granted:")
    for tool in grantable["tools"]:
        print(f"  {tool['name']:<28} {tool['description'][:80]}")


def cmd_approve(client: httpx.Client, args: argparse.Namespace) -> None:
    detail = _call(
        client,
        "POST",
        f"/external-agents/{args.agent_id}/approve",
        json={"features": _split(args.features), "tools": _split(args.tools)},
    )
    print(f"{detail['slug']} is {detail['status']}")
    print(f"  features: {detail['feature_grants'] or '—'}")
    print(f"  tools:    {detail['tool_grants'] or '—'}")
    print(f"\nNot done yet — nobody can use it until its identifier is on a role:")
    print(f"  python scripts/kvark_admin.py permit {detail['slug']} --role <role name>")


def cmd_grants(client: httpx.Client, args: argparse.Namespace) -> None:
    detail = _call(
        client,
        "PUT",
        f"/external-agents/{args.agent_id}/grants",
        json={"features": _split(args.features), "tools": _split(args.tools)},
    )
    print(f"features: {detail['feature_grants'] or '—'}")
    print(f"tools:    {detail['tool_grants'] or '—'}")


def cmd_disable(client: httpx.Client, args: argparse.Namespace) -> None:
    detail = _call(client, "POST", f"/external-agents/{args.agent_id}/disable")
    print(f"{detail['slug']} is {detail['status']} — its next call is refused with agent_disabled")


def cmd_uninstall(client: httpx.Client, args: argparse.Namespace) -> None:
    """Remove an agent outright. The slug is *not* released — it stays taken forever.

    The way to clear a pending queue that has hit `registration_capacity`: the caps count
    rows awaiting a decision, so removing or deciding on what is queued is what frees space.
    """
    _call(client, "DELETE", f"/external-agents/{args.agent_id}")
    print(f"agent {args.agent_id} removed. Its key stops working; its slug stays taken.")


def cmd_manifests(client: httpx.Client, args: argparse.Namespace) -> None:
    versions = _call(client, "GET", f"/external-agents/{args.agent_id}/manifests")
    for version in versions:
        print(f"[{version['id']}] {version['status']:<10} version={version['version']} at {version['submitted_at']}")
        if version["changelog"]:
            print(f"     changelog: {version['changelog']}")
        print(f"     submitted: {json.dumps(version['submitted'], ensure_ascii=False)}")


def cmd_accept(client: httpx.Client, args: argparse.Namespace) -> None:
    detail = _call(
        client,
        "POST",
        f"/external-agents/{args.agent_id}/manifests/{args.manifest_id}/approve",
        json={"features": _split(args.features), "tools": _split(args.tools)},
    )
    print(f"{detail['slug']} now runs version {detail['approved_version']}")
    print(f"  features: {detail['feature_grants'] or '—'}")
    print(f"  tools:    {detail['tool_grants'] or '—'}")


def cmd_reject(client: httpx.Client, args: argparse.Namespace) -> None:
    detail = _call(
        client,
        "POST",
        f"/external-agents/{args.agent_id}/manifests/{args.manifest_id}/reject",
        json={"reason": args.reason},
    )
    print(f"Rejected. {detail['slug']} goes on running version {detail['approved_version']}.")


def _grant_to_role(client: httpx.Client, role_name: str, readable: set[str], writable: set[str]) -> str:
    """Add identifiers to a role, keeping everything it already holds.

    The roles endpoint *replaces* a role's feature set rather than patching it, so the
    current set has to be read back and re-sent. Sending only the new identifier would
    strip every other permission the role has — a quiet way to lock an organisation out of
    its own product.

    An identifier the catalog does not know is refused here rather than sent, because the
    endpoint answers 400 for the whole request — and the request is a replacement, so the
    failure is silent about the fact that nothing changed.
    """
    catalog = _call(client, "POST", "/permissions/features/list")
    by_id = {item["id"]: item["identifier"] for group in catalog for item in group["items"]}
    known = set(by_id.values())

    missing = (readable | writable) - known
    if missing:
        raise AdminError(f"Not in the feature catalog: {sorted(missing)}")

    roles = _call(client, "POST", "/permissions/roles/list")
    role = next((item for item in roles if item["name"].lower() == role_name.lower()), None)
    if role is None:
        raise AdminError(f"No role named {role_name!r}. Roles: {sorted(item['name'] for item in roles)}")

    held = {by_id[perm["feature_id"]] for perm in role["feature_permissions"] if perm["feature_id"] in by_id}
    held_writable = {
        by_id[perm["feature_id"]]
        for perm in role["feature_permissions"]
        if perm.get("can_write") and perm["feature_id"] in by_id
    }

    _call(
        client,
        "PATCH",
        f"/permissions/roles/{role['id']}",
        json={
            "name": role["name"],
            "description": role.get("description") or "",
            "status": role.get("status") or "Active",
            "feature_permissions": sorted(held | readable | writable),
            "writable_features": sorted(held_writable | writable),
        },
    )
    return role["name"]


def cmd_bootstrap(client: httpx.Client, args: argparse.Namespace) -> None:
    """Grant a role the umbrella permission.

    A migration puts ``feature-external-agents`` on the ``Administrator`` role, so a
    deployment built from migrations needs none of this. It is still the way to reach any
    other role, and the way to repair a deployment that predates that migration — where the
    agents admin page answers 403 for everyone including the seeded admin, and so does every
    gateway call.

    Read and write are separate here, unlike the other product features: read means "act
    through an agent", write means "administer them". Both are granted, because the person
    running this walkthrough needs to do both.
    """
    name = _grant_to_role(
        client, args.role, readable={"feature-external-agents"}, writable={"feature-external-agents"}
    )
    print(f"{name} now holds feature-external-agents (read + write).")
    print("Read = act through an agent. Write = administer them. Sign in again to pick up the change.")


def cmd_role_feature(client: httpx.Client, args: argparse.Namespace) -> None:
    """Grant a role any catalog feature by identifier.

    Needed more often than it looks on a fresh deployment: the seed migration grants the
    Administrator role the features that existed when it was written, and every feature
    added by a later migration — `feature-context-board` and `feature-esms` among them —
    is granted to nobody. An agent holding a capability the person lacks answers
    `user_feature_missing`, which reads like an agent problem and is not one.
    """
    identifiers = set(_split(args.identifiers))
    name = _grant_to_role(
        client, args.role, readable=identifiers, writable=identifiers if args.write else set()
    )
    print(f"{name} now holds: {', '.join(sorted(identifiers))}{' (read + write)' if args.write else ''}")
    print("Sign in again to pick up the change — an existing token carries the old set.")


def cmd_permit(client: httpx.Client, args: argparse.Namespace) -> None:
    """Put one agent's identifier on a role, so people in it can act through that agent."""
    identifier = f"feature-external-agent-{args.agent_slug}"
    try:
        name = _grant_to_role(client, args.role, readable={identifier, "feature-external-agents"}, writable=set())
    except AdminError as failure:
        if identifier in str(failure):
            raise AdminError(
                f"{identifier!r} is not in the catalog yet. Approve the agent first — that is what creates the row."
            ) from failure
        raise
    print(f"{name} now holds {identifier}.")
    print("Anyone in that role can act through the agent — subject to their own feature permissions.")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every registered agent and what it was granted")
    sub.add_parser("capabilities", help="what can be granted on this deployment")

    show = sub.add_parser("show", help="one agent in full")
    show.add_argument("agent_id", type=int)

    approve = sub.add_parser("approve", help="approve a registration with capabilities")
    approve.add_argument("agent_id", type=int)
    approve.add_argument("--features", default="feature-chat,feature-search,feature-context-board")
    approve.add_argument("--tools", default="")

    grants = sub.add_parser("grants", help="replace what an agent may do")
    grants.add_argument("agent_id", type=int)
    grants.add_argument("--features", default="")
    grants.add_argument("--tools", default="")

    bootstrap = sub.add_parser("bootstrap", help="grant a role feature-external-agents (Administrator already has it)")
    bootstrap.add_argument("--role", default="Administrator")

    role_feature = sub.add_parser("role-feature", help="grant a role any catalog feature by identifier")
    role_feature.add_argument("identifiers", help="comma separated, e.g. feature-context-board,feature-esms")
    role_feature.add_argument("--role", default="Administrator")
    role_feature.add_argument("--write", action="store_true", help="also grant write")

    permit = sub.add_parser("permit", help="put the agent on a role so people can use it")
    permit.add_argument("agent_slug")
    permit.add_argument("--role", default="Administrator")

    manifests = sub.add_parser("manifests", help="every version this agent has submitted")
    manifests.add_argument("agent_id", type=int)

    accept = sub.add_parser("accept", help="put one stored manifest in force (also: roll back)")
    accept.add_argument("agent_id", type=int)
    accept.add_argument("manifest_id", type=int)
    accept.add_argument("--features", default="feature-chat,feature-search,feature-context-board")
    accept.add_argument("--tools", default="")

    reject = sub.add_parser("reject", help="refuse a submitted manifest")
    reject.add_argument("agent_id", type=int)
    reject.add_argument("manifest_id", type=int)
    reject.add_argument("--reason", default="")

    uninstall = sub.add_parser("uninstall", help="remove an agent (clears a pending queue)")
    uninstall.add_argument("agent_id", type=int)

    disable = sub.add_parser("disable", help="the kill switch")
    disable.add_argument("agent_id", type=int)

    args = parser.parse_args()
    handlers = {
        "list": cmd_list,
        "show": cmd_show,
        "capabilities": cmd_capabilities,
        "approve": cmd_approve,
        "grants": cmd_grants,
        "bootstrap": cmd_bootstrap,
        "role-feature": cmd_role_feature,
        "permit": cmd_permit,
        "manifests": cmd_manifests,
        "accept": cmd_accept,
        "reject": cmd_reject,
        "uninstall": cmd_uninstall,
        "disable": cmd_disable,
    }

    try:
        with httpx.Client(timeout=30.0, verify=_verify_tls()) as client:
            client.headers["Authorization"] = f"Bearer {_token(client)}"
            handlers[args.command](client, args)
    except AdminError as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    except httpx.HTTPError as unreachable:
        print(f"error: KVARK did not answer at {API} — {unreachable}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
