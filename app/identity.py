"""Getting a token for the person this agent acts for.

The gateway needs a bearer token saying *who* the agent is acting for, and it accepts a
KVARK-issued access token. There is no OAuth flow to sit in front of that yet, so this
application exchanges a person's KVARK credentials for one directly.

That is the compromise in this design and it is worth naming: a partner application that
holds KVARK passwords is not the shape a real integration should ship. The gateway already
carries the better path — ``IdpTokenAuthenticator`` accepts a Microsoft-AD token when
``AGENT_GATEWAY_IDP_ISSUER`` / ``_AUDIENCE`` / ``_JWKS_URL`` are configured — so phase two
replaces this file with a token received from the directory, and nothing else in this
application changes: everything downstream already treats the token as opaque.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import httpx
import jwt


@dataclass(frozen=True)
class Identity:
    """A signed-in person, as this agent knows them."""

    label: str
    token: str
    expires_at: datetime.datetime | None

    @property
    def expires_in(self) -> str:
        """How long the token has left, for the console header."""
        if self.expires_at is None:
            return "unknown"
        remaining = self.expires_at - datetime.datetime.now(datetime.UTC)
        if remaining.total_seconds() <= 0:
            return "expired"
        hours, seconds = divmod(int(remaining.total_seconds()), 3600)
        return f"{hours}h {seconds // 60}m" if hours else f"{seconds // 60}m"


class IdentityError(Exception):
    """The credentials were not accepted, or KVARK could not be reached."""


def token_expiry(token: str) -> datetime.datetime | None:
    """The token's own expiry, read without verifying it.

    Deliberately unverified: this agent is not a party to that signature and has no key to
    check it with. The value is used to render a countdown and to decide when to ask the
    person to sign in again — KVARK verifies the token on every call regardless, so being
    wrong here costs a redundant login and nothing else.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(exp, datetime.UTC)


async def sign_in(api_base: str, identifier: str, password: str) -> Identity:
    """Exchange KVARK credentials for an access token.

    Raises:
        IdentityError: the credentials were refused, or KVARK did not answer.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{api_base}/auth/login", json={"identifier": identifier, "password": password}
            )
    except httpx.HTTPError as unreachable:
        raise IdentityError(f"KVARK did not answer: {unreachable}") from unreachable

    if response.status_code != 200:
        detail: Any = ""
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = response.text[:200]
        raise IdentityError(str(detail) or f"Sign-in refused ({response.status_code}).")

    body = response.json()
    token = body.get("access_token") or body.get("token")
    if not isinstance(token, str) or not token:
        raise IdentityError("KVARK accepted the sign-in but returned no access token.")
    return Identity(label=identifier, token=token, expires_at=token_expiry(token))
