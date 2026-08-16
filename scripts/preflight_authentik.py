#!/usr/bin/env python
"""Preflight checks for the Authentik-OIDC (multi-user) connector mode.

Validates every leg of the chain — env, Authentik discovery, client credentials,
scopes, redirect-URI registration, Plane reachability, Redis, PAT encryption, and
provider construction — *before* you wire the connector into Claude.ai, where
failures surface only as opaque OAuth errors in a browser.

Usage:
    export $(grep -v '^#' .env.authentik | xargs)
    python scripts/preflight_authentik.py

Optionally set PLANE_TEST_PAT to a real Plane Personal Access Token to also verify
the Plane API leg end-to-end.

Read-only: it creates nothing in Authentik or Plane. The client-credential probe
deliberately sends an invalid authorization code and reads the error type back.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlencode

import httpx

TIMEOUT = 10

_results: list[tuple[str, str, str]] = []  # (status, title, detail)

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def record(status: str, title: str, detail: str = "") -> None:
    _results.append((status, title, detail))
    mark = {OK: "\033[32m ok \033[0m", WARN: "\033[33mwarn\033[0m", FAIL: "\033[31mFAIL\033[0m"}[status]
    print(f"[{mark}] {title}")
    if detail:
        for line in detail.splitlines():
            print(f"       {line}")


def public_base() -> str:
    return (os.getenv("MCP_PUBLIC_BASE_URL") or os.getenv("PLANE_OAUTH_PROVIDER_BASE_URL") or "").rstrip("/")


def prefix() -> str:
    return os.getenv("MCP_PATH_PREFIX") or ""


def oauth_callback_url() -> str:
    """Upstream redirect_uri used by OAuthProxy: {base_url}/auth/callback."""
    return f"{public_base()}{prefix()}/http/auth/callback"


def provision_callback_url() -> str:
    """The provisioning page runs its own separate Authentik login."""
    return f"{public_base()}{prefix()}/http/provision/callback"


# --------------------------------------------------------------------------- #
# 1. Environment
# --------------------------------------------------------------------------- #
def check_env() -> bool:
    required = ["AUTHENTIK_CLIENT_ID", "AUTHENTIK_CLIENT_SECRET"]
    missing = [k for k in required if not os.getenv(k)]

    has_discovery = bool(os.getenv("AUTHENTIK_CONFIG_URL")) or bool(
        os.getenv("AUTHENTIK_BASE_URL") and os.getenv("AUTHENTIK_APP_SLUG")
    )
    if not has_discovery:
        missing.append("AUTHENTIK_CONFIG_URL (or AUTHENTIK_BASE_URL + AUTHENTIK_APP_SLUG)")

    if missing:
        record(FAIL, "Environment", "Missing: " + ", ".join(missing))
        return False

    if not public_base():
        record(
            FAIL,
            "Environment",
            "MCP_PUBLIC_BASE_URL is not set. It must be the public HTTPS origin of this\n"
            "server — Claude.ai web cannot redirect to localhost.",
        )
        return False

    if not public_base().startswith("https://"):
        record(WARN, "Environment", f"MCP_PUBLIC_BASE_URL is not HTTPS: {public_base()}")

    if not os.getenv("MCP_PAT_ENCRYPTION_KEY"):
        record(
            WARN,
            "Environment",
            "MCP_PAT_ENCRYPTION_KEY unset — the PAT key will be derived from\n"
            "AUTHENTIK_CLIENT_SECRET, so rotating that secret orphans every stored PAT.",
        )

    record(OK, "Environment", f"public base: {public_base()}{prefix()}")
    return True


# --------------------------------------------------------------------------- #
# 2. Authentik discovery
# --------------------------------------------------------------------------- #
def check_discovery() -> dict | None:
    from plane_mcp.auth import resolve_config_url

    try:
        url = resolve_config_url()
    except ValueError as exc:
        record(FAIL, "Authentik discovery", str(exc))
        return None

    try:
        resp = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
    except httpx.RequestError as exc:
        record(FAIL, "Authentik discovery", f"{url}\nUnreachable: {exc}")
        return None

    if resp.status_code != 200:
        record(
            FAIL,
            "Authentik discovery",
            f"{url}\nHTTP {resp.status_code} — check AUTHENTIK_APP_SLUG matches the\n"
            "Application slug in Authentik (not the Provider name).",
        )
        return None

    try:
        cfg = resp.json()
    except ValueError:
        record(FAIL, "Authentik discovery", f"{url}\nResponse was not JSON.")
        return None

    record(
        OK,
        "Authentik discovery",
        f"issuer: {cfg.get('issuer')}\njwks:   {cfg.get('jwks_uri')}",
    )
    return cfg


# --------------------------------------------------------------------------- #
# 3. Scopes
# --------------------------------------------------------------------------- #
def check_scopes(cfg: dict) -> None:
    supported = set(cfg.get("scopes_supported") or [])
    if not supported:
        record(WARN, "Scopes", "Discovery lists no scopes_supported; cannot verify.")
        return

    needed = {"openid", "email", "profile", "offline_access"}
    missing = needed - supported
    if missing:
        detail = "Missing from the provider: " + ", ".join(sorted(missing))
        if "offline_access" in missing:
            detail += (
                "\noffline_access is required for a refresh token. Without it the\n"
                "connector dies when the access token expires and cannot renew.\n"
                "In Authentik: Provider > Advanced > Scopes, add the offline_access mapping."
            )
        record(FAIL, "Scopes", detail)
    else:
        record(OK, "Scopes", "openid, email, profile, offline_access all offered")


# --------------------------------------------------------------------------- #
# 4. Client credentials
# --------------------------------------------------------------------------- #
def check_client_credentials(cfg: dict) -> None:
    """Probe the token endpoint with a deliberately invalid authorization code.

    Authentik authenticates the *client* before it validates the code, so the error
    it returns distinguishes the two:
      invalid_grant  -> client_id/secret are correct (the code was rejected, as expected)
      invalid_client -> client_id/secret are wrong
    """
    token_endpoint = cfg.get("token_endpoint")
    if not token_endpoint:
        record(WARN, "Client credentials", "Discovery exposes no token_endpoint.")
        return

    try:
        resp = httpx.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": "preflight-intentionally-invalid",
                "redirect_uri": oauth_callback_url(),
            },
            auth=(os.getenv("AUTHENTIK_CLIENT_ID", ""), os.getenv("AUTHENTIK_CLIENT_SECRET", "")),
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        record(FAIL, "Client credentials", f"Token endpoint unreachable: {exc}")
        return

    try:
        error = (resp.json() or {}).get("error", "")
    except ValueError:
        error = ""

    if error == "invalid_grant":
        record(OK, "Client credentials", "client_id + client_secret accepted by Authentik")
    elif error == "invalid_client" or resp.status_code == 401:
        record(
            FAIL,
            "Client credentials",
            "Authentik rejected the client. Check AUTHENTIK_CLIENT_ID / AUTHENTIK_CLIENT_SECRET,\n"
            "and that the provider's client type is Confidential.",
        )
    else:
        record(WARN, "Client credentials", f"Unexpected response: HTTP {resp.status_code} error={error!r}")


# --------------------------------------------------------------------------- #
# 5. Redirect URIs
# --------------------------------------------------------------------------- #
def check_redirect_uris(cfg: dict) -> None:
    """Ask Authentik to start an authorize flow for each redirect URI we use.

    An unregistered redirect_uri is refused before login; a registered one gets a
    login/consent page or a redirect to the flow executor.
    """
    authorize_endpoint = cfg.get("authorization_endpoint")
    if not authorize_endpoint:
        record(WARN, "Redirect URIs", "Discovery exposes no authorization_endpoint.")
        return

    for label, redirect_uri in (
        ("connector", oauth_callback_url()),
        ("provision page", provision_callback_url()),
    ):
        params = {
            "client_id": os.getenv("AUTHENTIK_CLIENT_ID", ""),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": "preflight",
        }
        try:
            resp = httpx.get(
                f"{authorize_endpoint}?{urlencode(params)}",
                timeout=TIMEOUT,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            record(WARN, f"Redirect URI ({label})", f"{redirect_uri}\nCould not probe: {exc}")
            continue

        body = resp.text.lower()
        rejected = "redirect_uri" in body and ("invalid" in body or "not allowed" in body or "mismatch" in body)
        if rejected or resp.status_code == 400:
            record(
                FAIL,
                f"Redirect URI ({label})",
                f"{redirect_uri}\nAuthentik refused this redirect URI. Add it verbatim to the\n"
                "provider's Redirect URIs (Strict match).",
            )
        else:
            record(OK, f"Redirect URI ({label})", redirect_uri)


# --------------------------------------------------------------------------- #
# 6. Plane API
# --------------------------------------------------------------------------- #
def check_plane() -> None:
    base = (os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL") or "").rstrip("/")
    if not base:
        record(FAIL, "Plane API", "Neither PLANE_BASE_URL nor PLANE_INTERNAL_BASE_URL is set.")
        return

    pat = os.getenv("PLANE_TEST_PAT")
    url = f"{base}/api/v1/users/me/"
    if not pat:
        record(
            WARN,
            "Plane API",
            f"{url}\nSet PLANE_TEST_PAT to a real Plane token to verify this leg\n"
            "(this is exactly the call the provision page makes to validate a pasted PAT).",
        )
        return

    try:
        resp = httpx.get(url, headers={"x-api-key": pat}, timeout=TIMEOUT)
    except httpx.RequestError as exc:
        record(FAIL, "Plane API", f"{url}\nUnreachable: {exc}")
        return

    if resp.status_code == 200:
        who = ""
        try:
            data = resp.json()
            who = data.get("email") or data.get("display_name") or ""
        except ValueError:
            pass
        record(OK, "Plane API", f"PAT authenticated as {who or 'a valid user'}")
    else:
        record(FAIL, "Plane API", f"{url}\nHTTP {resp.status_code} — token rejected or wrong base URL.")


# --------------------------------------------------------------------------- #
# 7. Redis + PAT encryption
# --------------------------------------------------------------------------- #
def check_pat_store() -> None:
    host, port = os.getenv("REDIS_HOST"), os.getenv("REDIS_PORT")
    if not (host and port):
        record(
            WARN,
            "PAT store",
            "REDIS_HOST/REDIS_PORT unset — the store falls back to memory and every\n"
            "user's link is lost on restart. Configure Redis for a real deployment.",
        )
        return

    try:
        from plane_mcp.pat_store import build_pat_store

        store = build_pat_store()
        probe = "preflight-probe-subject"
        store.set_pat(probe, "plane_api_preflight_value")
        roundtrip = store.get_pat(probe)
        store.delete(probe)
    except Exception as exc:  # noqa: BLE001 - report any backend/crypto failure
        record(FAIL, "PAT store", f"{type(exc).__name__}: {exc}")
        return

    if roundtrip == "plane_api_preflight_value":
        record(OK, "PAT store", f"Redis {host}:{port} — encrypt/decrypt round-trip verified")
    else:
        record(FAIL, "PAT store", "Round-trip returned the wrong value; check MCP_PAT_ENCRYPTION_KEY.")


# --------------------------------------------------------------------------- #
# 8. Provider construction
# --------------------------------------------------------------------------- #
def check_provider_builds() -> None:
    """Build the real provider + tool surface, as __main__ does on boot.

    __main__ catches failures here and silently falls back to header-auth-only,
    so a break in this path leaves a server that boots fine and quietly serves
    nobody. Surface it loudly instead.
    """
    try:
        from plane_mcp.server import get_authentik_oauth_mcp

        mcp = get_authentik_oauth_mcp(prefix() + "/http")
    except Exception as exc:  # noqa: BLE001 - this is the check
        record(
            FAIL,
            "Provider construction",
            f"{type(exc).__name__}: {exc}\n"
            "__main__ would swallow this and fall back to header-auth-only —\n"
            "the web connector would never mount.",
        )
        return

    record(OK, "Provider construction", f"AuthentikOIDCProvider built; MCP endpoint {prefix()}/http/mcp")

    try:
        routes = mcp.auth.get_well_known_routes(mcp_path="/mcp")
        paths = ", ".join(r.path for r in routes) or "(none)"
        record(OK, "Discovery routes", paths)
    except Exception as exc:  # noqa: BLE001
        record(WARN, "Discovery routes", f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("\nPreflight — Authentik OIDC multi-user connector\n" + "=" * 52)

    if not check_env():
        print("\nFix the environment first; later checks depend on it.\n")
        return 1

    cfg = check_discovery()
    if cfg:
        check_scopes(cfg)
        check_client_credentials(cfg)
        check_redirect_uris(cfg)

    check_plane()
    check_pat_store()
    check_provider_builds()

    fails = sum(1 for s, _, _ in _results if s == FAIL)
    warns = sum(1 for s, _, _ in _results if s == WARN)
    print("=" * 52)
    print(f"{len(_results)} checks — {fails} failed, {warns} warnings\n")

    if fails:
        print("Resolve the failures above before adding the connector in Claude.ai.\n")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
