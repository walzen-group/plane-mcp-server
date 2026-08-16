"""Per-user workspace switching on the `workspace` tool.

A Plane personal access token authenticates a *user*, not a workspace, so one
linked PAT already spans every workspace that user belongs to. `set_active` moves
the caller between them by rewriting only *their* PAT-store mapping — the switch
must never leak into another user's context, and must be refused on connections
carrying no per-user identity (stdio / header auth), where the workspace comes
from server configuration.

No network: the Plane client and the PAT store are fakes.
"""

from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet
from fastmcp import Client, FastMCP

from plane_mcp.pat_store import PatStore, derive_fernet_key
from plane_mcp.tools import workspace as workspace_mod


class FakeWorkspaces:
    """Membership check stand-in: only ``allowed`` slugs resolve."""

    def __init__(self, allowed: set[str]):
        self.allowed = allowed
        self.checked: list[str] = []

    def get_members_lite(self, workspace_slug, params):
        self.checked.append(workspace_slug)
        if workspace_slug not in self.allowed:
            raise RuntimeError("404 workspace not found")
        return {"results": []}


class FakeClient:
    def __init__(self, allowed: set[str]):
        self.workspaces = FakeWorkspaces(allowed)


def _error(result) -> str:
    """The refusal text a model would read back from a guard clause."""
    return result.content[0].text


def _fresh_store() -> PatStore:
    return PatStore(fernet=Fernet(derive_fernet_key("workspace-active-test")))


def _call(monkeypatch, args, *, sub, store, client, active="old-ws"):
    """Invoke the `workspace` tool through a real FastMCP client with fakes wired in."""
    monkeypatch.setattr(workspace_mod, "get_authenticated_sub", lambda: sub)
    monkeypatch.setattr(workspace_mod, "get_pat_store", lambda: store)
    monkeypatch.setattr(workspace_mod, "get_plane_client_context", lambda: (client, active))

    async def run():
        mcp = FastMCP("test")
        workspace_mod.register(mcp)
        async with Client(mcp) as c:
            return await c.call_tool(workspace_mod.NAME, args)

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run())


class TestSetActive:
    def test_switch_persists_for_that_user_only(self, monkeypatch):
        """The new slug lands under the caller's sub; other users are untouched."""
        store = _fresh_store()
        store.set_workspace("user-a", "old-ws")
        store.set_workspace("user-b", "b-ws")

        result = _call(
            monkeypatch,
            {"action": "set_active", "workspace_slug": "new-ws"},
            sub="user-a",
            store=store,
            client=FakeClient({"new-ws"}),
        )

        assert result.data.workspace_slug == "new-ws"
        assert result.data.switchable is True
        assert store.get_workspace("user-a") == "new-ws"
        # Isolation: another user's workspace must not move.
        assert store.get_workspace("user-b") == "b-ws"

    def test_membership_is_verified_before_persisting(self, monkeypatch):
        """A slug the caller can't reach is refused and never stored."""
        store = _fresh_store()
        store.set_workspace("user-a", "old-ws")
        client = FakeClient({"only-this-one"})

        result = _call(
            monkeypatch,
            {"action": "set_active", "workspace_slug": "typo-ws"},
            sub="user-a",
            store=store,
            client=client,
        )

        assert _error(result).startswith("Error:") and "typo-ws" in _error(result)

        assert client.workspaces.checked == ["typo-ws"]
        # The old mapping survives a failed switch.
        assert store.get_workspace("user-a") == "old-ws"

    def test_refused_without_per_user_identity(self, monkeypatch):
        """stdio / header-auth connections take their workspace from configuration."""
        store = _fresh_store()
        client = FakeClient({"new-ws"})

        result = _call(
            monkeypatch,
            {"action": "set_active", "workspace_slug": "new-ws"},
            sub=None,
            store=store,
            client=client,
        )

        assert "fixed by server configuration" in _error(result)

        # No membership call and no write when there is no identity to key on.
        assert client.workspaces.checked == []

    def test_blank_slug_refused(self, monkeypatch):
        store = _fresh_store()
        store.set_workspace("user-a", "old-ws")

        result = _call(
            monkeypatch,
            {"action": "set_active", "workspace_slug": "   "},
            sub="user-a",
            store=store,
            client=FakeClient({"new-ws"}),
        )

        assert _error(result) == "Error: action 'set_active' requires: workspace_slug."

        assert store.get_workspace("user-a") == "old-ws"

    def test_slug_is_trimmed(self, monkeypatch):
        store = _fresh_store()

        result = _call(
            monkeypatch,
            {"action": "set_active", "workspace_slug": "  new-ws  "},
            sub="user-a",
            store=store,
            client=FakeClient({"new-ws"}),
        )

        assert result.data.workspace_slug == "new-ws"
        assert store.get_workspace("user-a") == "new-ws"


class TestGetActive:
    def test_reports_active_workspace_and_switchable_for_oidc(self, monkeypatch):
        result = _call(
            monkeypatch,
            {"action": "get_active"},
            sub="user-a",
            store=_fresh_store(),
            client=FakeClient(set()),
            active="acme",
        )

        assert result.data.workspace_slug == "acme"
        assert result.data.switchable is True

    def test_not_switchable_without_per_user_identity(self, monkeypatch):
        result = _call(
            monkeypatch,
            {"action": "get_active"},
            sub=None,
            store=_fresh_store(),
            client=FakeClient(set()),
            active="configured-ws",
        )

        assert result.data.workspace_slug == "configured-ws"
        assert result.data.switchable is False
