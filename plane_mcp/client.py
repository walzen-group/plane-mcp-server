"""Plane client initialization for MCP server."""

import os
from typing import Any, NamedTuple

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient

from plane_mcp.pat_store import get_pat_store

logger = get_logger(__name__)


class PlaneClientContext(NamedTuple):
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def provision_url() -> str:
    """Public URL of the self-service provisioning page (CONTRACT §E).

    ``{MCP_PUBLIC_BASE_URL}{MCP_PATH_PREFIX}/http/provision``. Falls back to
    ``PLANE_OAUTH_PROVIDER_BASE_URL`` for the public base, matching server.py.
    """
    base = (os.getenv("MCP_PUBLIC_BASE_URL") or os.getenv("PLANE_OAUTH_PROVIDER_BASE_URL") or "").rstrip("/")
    prefix = os.getenv("MCP_PATH_PREFIX") or ""
    return f"{base}{prefix}/http/provision"


def plane_pat_create_url() -> str:
    """Deep link to the Plane web page where a user creates a PAT (CONTRACT §G).

    Prefers ``PLANE_WEB_URL``; otherwise derives it from ``PLANE_BASE_URL`` by
    stripping a trailing ``/api``. Mirrors ``provisioning._plane_web_url`` so the
    first-run error and the provision page point at the same place.
    """
    web = os.getenv("PLANE_WEB_URL")
    if not web:
        base = (os.getenv("PLANE_BASE_URL") or "").rstrip("/")
        web = base[: -len("/api")] if base.endswith("/api") else base
    web = (web or "").rstrip("/")
    return f"{web}/settings/profile/api-tokens/" if web else ""


def get_authenticated_sub() -> str | None:
    """Return the verified OIDC subject for this request, or None.

    Only ``authentik_oidc`` requests carry a per-user identity that the PAT store
    is keyed by. stdio, header/PAT and legacy Plane-Cloud OAuth requests have no
    such per-user mapping, so they return None.
    """
    stored_access_token: AccessToken | None = get_access_token()
    if not stored_access_token:
        return None
    claims = stored_access_token.claims
    if claims.get("auth_method") != "authentik_oidc":
        return None
    return claims.get("sub") or None


def _workspace_slug(token: AccessToken | None) -> str:
    """The slug a connection is bound to: its token's claim, else the environment."""
    if token:
        return token.claims.get("workspace_slug", "")
    return os.getenv("PLANE_WORKSPACE_SLUG", "")


def _connected_via(token: AccessToken | None) -> str:
    """How a connection authenticated: its token's claim, else the environment."""
    if token:
        return token.claims.get("auth_method", "oauth")
    return "environment"


def current_workspace() -> dict[str, Any]:
    """The workspace this connection is bound to, as far as its credentials say."""
    token = get_access_token()
    detail = (token.claims.get("workspace") if token else None) or {}
    return {
        "slug": _workspace_slug(token),
        "id": detail.get("id"),
        "name": detail.get("name"),
        "connected_via": _connected_via(token),
    }


def get_plane_client_context() -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Authentication is resolved from the MCP request's access token (set by the
    active auth provider) or from environment variables (stdio mode). The
    ``auth_method`` claim selects how the Plane credential is obtained:

    - ``authentik_oidc``: the verified IdP identity (``sub``) is mapped to that
      user's Plane PAT via the PAT store. The IdP token MUST NOT reach the Plane
      API; only the resolved PAT is used. An unlinked user gets an actionable
      ``ToolError`` pointing at the provisioning page.
    - ``api_key_env`` / ``api_key_header``: the token is a Plane API key.
    - ``oauth`` (legacy Plane-Cloud): the token is passed through as the bearer.

    Environment variables:
    - PLANE_INTERNAL_BASE_URL: Internal URL for Plane API (preferred for server-to-server calls)
    - PLANE_BASE_URL: Base URL for Plane API (fallback, default: https://api.plane.so)
    - PLANE_WORKSPACE_SLUG: Per-user workspace fallback.

    Returns:
        PlaneClientContext containing configured PlaneClient instance and workspace slug

    Raises:
        ToolError: If an OIDC-authenticated user has not yet linked a Plane PAT.
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None

    # Get access token from the auth provider (which handles all auth methods)
    stored_access_token: AccessToken | None = get_access_token()
    workspace_slug = _workspace_slug(stored_access_token)
    if stored_access_token:
        # Determine authentication method to use appropriate PlaneClient constructor
        claims = stored_access_token.claims
        auth_method = _connected_via(stored_access_token)
        token = stored_access_token.token

        if auth_method == "authentik_oidc":
            # Map the verified Authentik identity to the user's Plane PAT. The IdP
            # token is never a Plane credential and must not be forwarded.
            sub = claims.get("sub")
            pat = get_pat_store().get_pat(sub) if sub else None
            if not pat:
                pat_url = plane_pat_create_url()
                hint = f" Create a token at {pat_url} ." if pat_url else ""
                raise ToolError(
                    "Your Plane account isn't linked to this connector yet. Open "
                    f"{provision_url()} to link it — you'll paste a Plane Personal "
                    f"Access Token there.{hint} Then retry this request."
                )
            api_key = pat
            access_token = None  # IdP token MUST NOT reach the Plane API
            workspace_slug = get_pat_store().get_workspace(sub) or os.getenv("PLANE_WORKSPACE_SLUG", "")
        elif auth_method in ("api_key_env", "api_key_header"):
            api_key = token
            workspace_slug = claims.get("workspace_slug", "") or workspace_slug
        else:
            # Legacy Plane-Cloud OAuth pass-through, unchanged.
            access_token = token
            workspace_slug = claims.get("workspace_slug", "") or workspace_slug

    if access_token:
        client = PlaneClient(
            base_url=base_url,
            access_token=access_token,
        )
    else:
        client = PlaneClient(
            base_url=base_url,
            api_key=api_key,
        )

    return PlaneClientContext(
        client=client,
        workspace_slug=workspace_slug,
    )
