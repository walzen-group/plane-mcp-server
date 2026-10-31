"""The connected workspace, its feature flags, and which workspace the caller acts in."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastmcp import FastMCP
from plane.models.query_params import MemberListQueryParams
from plane.models.workspaces import WorkspaceFeature
from pydantic import BaseModel, Field

from plane_mcp.client import current_workspace, get_authenticated_sub, get_plane_client_context
from plane_mcp.pat_store import get_pat_store
from plane_mcp.toolkit import Action, build_annotations, build_description, missing

NAME = "workspace"
TITLE = "Workspace settings"


class ActiveWorkspace(BaseModel):
    """The workspace slug this connector currently acts in for the calling user."""

    workspace_slug: str = Field(description="Slug of the workspace tools currently operate on.")
    switchable: bool = Field(
        description=(
            "True when the caller can change it with set_active. False when the workspace "
            "is fixed by server configuration (stdio / header-auth connections)."
        )
    )


ACTIONS = (
    Action(
        "retrieve",
        note="the workspace this connection is bound to: slug, id, name and how it connected. "
        "id and name are known only on an OAuth connection and are null otherwise",
        read=True,
    ),
    Action("get_features", note="feature flags for the current workspace", read=True),
    Action(
        "update_features",
        optional=("project_grouping", "initiatives", "teams", "customers", "wiki", "pi"),
        note="only the flags you pass are changed",
    ),
    Action("get_active", note="which workspace tools currently act in, for you", read=True),
    Action(
        "set_active",
        requires=("workspace_slug",),
        note="switch yourself to another workspace you belong to; other users are unaffected",
    ),
)

FOOTER = (
    "For a project's feature flags use `project get_features` and `project update_features`.\n"
    "One workspace is active at a time per user. A Plane personal access token spans every "
    "workspace its owner belongs to, so `workspace set_active` needs no new credential and "
    "persists across conversations. On stdio and header-auth connections the workspace comes "
    "from server configuration and cannot be switched."
)

LEGACY = {
    "get_features": "get_features",
    "update_workspace_features": "update_features",
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "The connected workspace, its feature flags, and which workspace you act in.", ACTIONS, FOOTER
        ),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def workspace(
        action: Literal["retrieve", "get_features", "update_features", "get_active", "set_active"],
        # Tri-state throughout: False disables a feature, unset leaves it alone.
        project_grouping: bool | None = None,
        initiatives: bool | None = None,
        teams: bool | None = None,
        customers: bool | None = None,
        wiki: bool | None = None,
        pi: bool | None = None,
        workspace_slug: str | None = None,
    ) -> WorkspaceFeature | ActiveWorkspace | dict[str, Any] | str:
        if action == "retrieve":
            info = current_workspace()
            # Per-user workspace switching: an OIDC caller's slug lives in the PAT
            # store, not in its token, so report the one tools actually act in.
            if (sub := get_authenticated_sub()) is not None:
                info["slug"] = get_pat_store().get_workspace(sub) or os.getenv("PLANE_WORKSPACE_SLUG", "")
            return info

        client, active_slug = get_plane_client_context()

        if action == "get_features":
            return client.workspaces.get_features(workspace_slug=active_slug)

        if action == "get_active":
            return ActiveWorkspace(workspace_slug=active_slug, switchable=get_authenticated_sub() is not None)

        if action == "set_active":
            return _set_active(client, workspace_slug)

        flags = {
            "project_grouping": project_grouping,
            "initiatives": initiatives,
            "teams": teams,
            "customers": customers,
            "wiki": wiki,
            "pi": pi,
        }
        data = WorkspaceFeature(**{k: v for k, v in flags.items() if v is not None})
        return client.workspaces.update_features(workspace_slug=active_slug, data=data)


def _set_active(client, workspace_slug: str | None) -> ActiveWorkspace | str:
    """Point the caller — and only the caller — at another workspace.

    The mapping is keyed by the verified OIDC subject, so one user's switch can
    never move another's. Connections without such a subject take their workspace
    from configuration and are refused rather than silently ignored.

    The absent-parameter check comes first: it is the one refusal a caller can act
    on by retrying, and it must not be masked by the identity guard behind it.
    """
    slug = (workspace_slug or "").strip()
    if not slug:
        return missing("set_active", "workspace_slug")

    if (sub := get_authenticated_sub()) is None:
        return (
            "Error: this connection's workspace is fixed by server configuration and "
            "cannot be switched. Set PLANE_WORKSPACE_SLUG (stdio) or send the "
            "x-workspace-slug header (API-key HTTP) instead."
        )

    # Verify membership with the caller's own token before persisting, so a typo
    # doesn't leave every later tool call failing against a bogus workspace.
    try:
        client.workspaces.get_members_lite(workspace_slug=slug, params=MemberListQueryParams(per_page=1))
    except Exception:
        return (
            f"Error: could not access workspace '{slug}' with your Plane token — check "
            "the slug and that you are a member of it."
        )

    get_pat_store().set_workspace(sub, slug)
    return ActiveWorkspace(workspace_slug=slug, switchable=True)
