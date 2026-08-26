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

    python scripts/kvark_admin.py list
    python scripts/kvark_admin.py show <agent_id>
    python scripts/kvark_admin.py approve <agent_id> --features feature-chat,feature-search
    python scripts/kvark_admin.py permit <agent_slug> --role Admin
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

API = os.environ.get("KVARK_API_BASE", "http://localhost:8008/api").rstrip("/")
USER = os.environ.get("KVARK_ADMIN_USER", "admin")
PASSWORD = os.environ.get("KVARK_ADMIN_PASSWORD", "admin")


class AdminError(Exception):
    pass


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


def cmd_permit(client: httpx.Client, args: argparse.Namespace) -> None:
    """Put the agent's identifier on a role, alongside what that role already holds.

    The roles endpoint *replaces* a role's feature set rather than patching it, so the
    current set has to be read back and re-sent. Sending only the new identifier would
    strip every other permission the role has — which is a quiet way to lock an
    organisation out of its own product.
    """
    identifier = f"feature-external-agent-{args.agent_slug}"

    catalog = _call(client, "POST", "/permissions/features/list")
    by_id = {item["id"]: item["identifier"] for group in catalog for item in group["items"]}
    known = set(by_id.values())
    if identifier not in known:
        raise AdminError(
            f"{identifier!r} is not in the catalog. Approve the agent first — the row is created then."
        )

    roles = _call(client, "POST", "/permissions/roles/list")
    role = next((item for item in roles if item["name"].lower() == args.role.lower()), None)
    if role is None:
        raise AdminError(f"No role named {args.role!r}. Roles: {sorted(item['name'] for item in roles)}")

    current = [by_id[perm["feature_id"]] for perm in role["feature_permissions"] if perm["feature_id"] in by_id]
    writable = [
        by_id[perm["feature_id"]]
        for perm in role["feature_permissions"]
        if perm.get("can_write") and perm["feature_id"] in by_id
    ]
    if identifier in current:
        print(f"{role['name']} already holds {identifier}.")
        return

    # `feature-external-agents` is the umbrella permission every gateway call is checked
    # against, so it goes on too — the per-agent row alone is not enough. Filtered against
    # the catalog because an identifier the catalog does not know is a 400 for the whole
    # request, which would take the role's existing permissions down with it.
    additions = {identifier, "feature-external-agents"} & known
    wanted = sorted(set(current) | additions)
    _call(
        client,
        "PATCH",
        f"/permissions/roles/{role['id']}",
        json={
            "name": role["name"],
            "description": role.get("description") or "",
            "status": role.get("status") or "Active",
            "feature_permissions": wanted,
            "writable_features": sorted(set(writable)),
        },
    )
    print(f"{role['name']} now holds {identifier}.")
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

    permit = sub.add_parser("permit", help="put the agent on a role so people can use it")
    permit.add_argument("agent_slug")
    permit.add_argument("--role", default="Admin")

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

    disable = sub.add_parser("disable", help="the kill switch")
    disable.add_argument("agent_id", type=int)

    args = parser.parse_args()
    handlers = {
        "list": cmd_list,
        "show": cmd_show,
        "capabilities": cmd_capabilities,
        "approve": cmd_approve,
        "grants": cmd_grants,
        "permit": cmd_permit,
        "manifests": cmd_manifests,
        "accept": cmd_accept,
        "reject": cmd_reject,
        "disable": cmd_disable,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
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
