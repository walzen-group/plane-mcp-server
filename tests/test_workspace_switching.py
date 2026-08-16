"""Unit tests for per-user workspace switching (multi-workspace support).

A Plane personal access token authenticates a *user*, not a workspace, so one linked
PAT already spans every workspace that user belongs to. ``set_active_workspace`` moves
the caller between them by rewriting only *their* PAT-store mapping — the switch must
never leak into another user's context, and must be rejected on connections that carry
no per-user identity (stdio / header-auth), where the workspace comes from config.

No network: the Plane client and the PAT store are fakes.
"""

import asyncio

import pytest
from cryptography.fernet import Fernet
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from plane_mcp.pat_store import PatStore, derive_fernet_key
from plane_mcp.tools import workspaces as workspace_tools


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


def _fresh_store() -> PatStore:
    return PatStore(fernet=Fernet(derive_fernet_key("workspace-switch-test")))


def _call(monkeypatch, tool, args, *, sub, store, client, workspace="old-ws"):
    """Invoke a workspace tool through a real FastMCP client with fakes wired in."""
    monkeypatch.setattr(workspace_tools, "get_authenticated_sub", lambda: sub)
    monkeypatch.setattr(workspace_tools, "get_pat_store", lambda: store)
    monkeypatch.setattr(workspace_tools, "get_plane_client_context", lambda: (client, workspace))

    async def run():
        mcp = FastMCP("test")
        workspace_tools.register_workspace_tools(mcp)
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(run())


class TestSetActiveWorkspace:
    def test_switch_persists_for_that_user_only(self, monkeypatch):
        """The new slug lands under the caller's sub; other users are untouched."""
        store = _fresh_store()
        store.set_workspace("user-a", "old-ws")
        store.set_workspace("user-b", "b-ws")

        result = _call(
            monkeypatch,
            "set_active_workspace",
            {"workspace_slug": "new-ws"},
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
        """A slug the caller can't reach is rejected and never stored."""
        store = _fresh_store()
        store.set_workspace("user-a", "old-ws")
        client = FakeClient({"only-this-one"})

        with pytest.raises(ToolError, match="typo-ws"):
            _call(
                monkeypatch,
                "set_active_workspace",
                {"workspace_slug": "typo-ws"},
                sub="user-a",
                store=store,
                client=client,
            )

        assert client.workspaces.checked == ["typo-ws"]
        # The old mapping survives a failed switch.
        assert store.get_workspace("user-a") == "old-ws"

    def test_rejected_without_per_user_identity(self, monkeypatch):
        """stdio / header-auth connections take their workspace from config."""
        store = _fresh_store()
        client = FakeClient({"new-ws"})

        with pytest.raises(ToolError, match="fixed by server configuration"):
            _call(
                monkeypatch,
                "set_active_workspace",
                {"workspace_slug": "new-ws"},
                sub=None,
                store=store,
                client=client,
            )

        # No membership call and no write when there is no identity to key on.
        assert client.workspaces.checked == []

    def test_blank_slug_rejected(self, monkeypatch):
        store = _fresh_store()
        store.set_workspace("user-a", "old-ws")

        with pytest.raises(ToolError, match="must not be empty"):
            _call(
                monkeypatch,
                "set_active_workspace",
                {"workspace_slug": "   "},
                sub="user-a",
                store=store,
                client=FakeClient({"new-ws"}),
            )

        assert store.get_workspace("user-a") == "old-ws"

    def test_slug_is_trimmed(self, monkeypatch):
        store = _fresh_store()

        result = _call(
            monkeypatch,
            "set_active_workspace",
            {"workspace_slug": "  new-ws  "},
            sub="user-a",
            store=store,
            client=FakeClient({"new-ws"}),
        )

        assert result.data.workspace_slug == "new-ws"
        assert store.get_workspace("user-a") == "new-ws"


class TestGetActiveWorkspace:
    def test_reports_current_workspace_and_switchable_for_oidc(self, monkeypatch):
        result = _call(
            monkeypatch,
            "get_active_workspace",
            {},
            sub="user-a",
            store=_fresh_store(),
            client=FakeClient(set()),
            workspace="acme",
        )

        assert result.data.workspace_slug == "acme"
        assert result.data.switchable is True

    def test_not_switchable_without_per_user_identity(self, monkeypatch):
        result = _call(
            monkeypatch,
            "get_active_workspace",
            {},
            sub=None,
            store=_fresh_store(),
            client=FakeClient(set()),
            workspace="configured-ws",
        )

        assert result.data.workspace_slug == "configured-ws"
        assert result.data.switchable is False
