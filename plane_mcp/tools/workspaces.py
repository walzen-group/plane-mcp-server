"""Workspace-related tools for Plane MCP Server."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from plane.models.projects import ProjectFeature
from plane.models.query_params import MemberListQueryParams
from plane.models.workspaces import PaginatedWorkspaceMemberResponse, WorkspaceFeature
from pydantic import BaseModel, Field

from plane_mcp.client import get_authenticated_sub, get_plane_client_context
from plane_mcp.pat_store import get_pat_store


class ActiveWorkspace(BaseModel):
    """The workspace slug this connector currently acts in for the calling user."""

    workspace_slug: str = Field(description="Slug of the workspace tools currently operate on.")
    switchable: bool = Field(
        description=(
            "True when the caller can change it with set_active_workspace. False when the "
            "workspace is fixed by server configuration (stdio / header-auth modes)."
        )
    )


def register_workspace_tools(mcp: FastMCP) -> None:
    """Register all workspace-related tools with the MCP server."""

    @mcp.tool()
    def get_active_workspace() -> ActiveWorkspace:
        """
        Show which Plane workspace this connector is currently acting in.

        Every other tool operates on this workspace. Use set_active_workspace to
        change it (OAuth/SSO connections only).

        Returns:
            ActiveWorkspace with the current slug and whether it can be switched.
        """
        _, workspace_slug = get_plane_client_context()
        return ActiveWorkspace(workspace_slug=workspace_slug, switchable=get_authenticated_sub() is not None)

    @mcp.tool()
    def set_active_workspace(workspace_slug: str) -> ActiveWorkspace:
        """
        Switch which Plane workspace this connector acts in, for the calling user only.

        Your Plane Personal Access Token spans every workspace you belong to, so this
        just changes which one tools target. The choice is stored per user and persists
        across conversations until changed again. Other users are unaffected.

        Only available on OAuth/SSO connections; stdio and header/API-key connections
        take their workspace from server configuration.

        Args:
            workspace_slug: Slug of a workspace you are a member of (e.g. "my-workspace").

        Returns:
            ActiveWorkspace reflecting the newly selected workspace.

        Raises:
            ToolError: If the connection has no per-user identity, the slug is blank,
                or you are not a member of that workspace.
        """
        sub = get_authenticated_sub()
        if sub is None:
            raise ToolError(
                "This connection's workspace is fixed by server configuration and cannot be "
                "switched. Set PLANE_WORKSPACE_SLUG (stdio) or send the x-workspace-slug "
                "header (API-key HTTP) instead."
            )

        slug = workspace_slug.strip()
        if not slug:
            raise ToolError("workspace_slug must not be empty.")

        # Verify membership with the caller's own PAT before persisting, so a typo
        # doesn't leave every later tool call failing against a bogus workspace.
        client, _ = get_plane_client_context()
        try:
            client.workspaces.get_members_lite(workspace_slug=slug, params=MemberListQueryParams(per_page=1))
        except Exception as exc:
            raise ToolError(
                f"Could not access workspace '{slug}' with your Plane token — check the slug "
                "and that you are a member of it."
            ) from exc

        get_pat_store().set_workspace(sub, slug)
        return ActiveWorkspace(workspace_slug=slug, switchable=True)

    @mcp.tool()
    def get_workspace_members(
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        display_name: str | None = None,
        role_slug: str | None = None,
        is_active: bool | None = None,
        is_bot: bool | None = None,
        cursor: str | None = None,
        per_page: int | None = 100,
        order_by: str | None = None,
    ) -> PaginatedWorkspaceMemberResponse:
        """
        List members of the current workspace (filterable, paginated).

        Optional filters first_name/last_name/email/display_name (case-insensitive
        contains), role_slug (exact), is_active, is_bot — combined with AND.

        Args:
            cursor: Prior response's next_cursor; omit for first page.
            per_page: Results per page (1-1000, default 100).
            order_by: Sort field; prefix '-' for descending.

        Returns:
            Paginated envelope: results (members incl. role, role_slug,
            is_active, is_bot) + total_count, next_cursor, next_page_results.
        """
        client, workspace_slug = get_plane_client_context()
        params = MemberListQueryParams(
            first_name=first_name,
            last_name=last_name,
            email=email,
            display_name=display_name,
            role_slug=role_slug,
            is_active=is_active,
            is_bot=is_bot,
            cursor=cursor,
            per_page=per_page,
            order_by=order_by,
        )
        return client.workspaces.get_members_lite(workspace_slug=workspace_slug, params=params)

    @mcp.tool()
    def get_features(project_id: str | None = None) -> WorkspaceFeature | ProjectFeature:
        """
        Get feature flags.

        Returns a project's features if project_id is given, otherwise the
        workspace's features.

        Args:
            project_id: UUID of the project. Omit for workspace features.

        Returns:
            ProjectFeature when project_id is given, otherwise WorkspaceFeature.
        """
        client, workspace_slug = get_plane_client_context()
        if project_id is not None:
            return client.projects.get_features(workspace_slug=workspace_slug, project_id=project_id)
        return client.workspaces.get_features(workspace_slug=workspace_slug)

    @mcp.tool()
    def update_workspace_features(
        project_grouping: bool | None = None,
        initiatives: bool | None = None,
        teams: bool | None = None,
        customers: bool | None = None,
        wiki: bool | None = None,
        pi: bool | None = None,
    ) -> WorkspaceFeature:
        """
        Update features of the current workspace.

        Args:
            project_grouping: Enable/disable project grouping feature
            initiatives: Enable/disable initiatives feature
            teams: Enable/disable teams feature
            customers: Enable/disable customers feature
            wiki: Enable/disable wiki feature
            pi: Enable/disable PI (Program Increment) feature

        Returns:
            Updated WorkspaceFeature object
        """
        client, workspace_slug = get_plane_client_context()

        # Build data dict with only non-None values
        feature_data: dict[str, bool] = {}
        if project_grouping is not None:
            feature_data["project_grouping"] = project_grouping
        if initiatives is not None:
            feature_data["initiatives"] = initiatives
        if teams is not None:
            feature_data["teams"] = teams
        if customers is not None:
            feature_data["customers"] = customers
        if wiki is not None:
            feature_data["wiki"] = wiki
        if pi is not None:
            feature_data["pi"] = pi

        data = WorkspaceFeature(**feature_data)

        return client.workspaces.update_features(workspace_slug=workspace_slug, data=data)
