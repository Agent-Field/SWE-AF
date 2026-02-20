"""Shared prompt utility functions for the prompts package."""

from __future__ import annotations

from swe_af.execution.schemas import WorkspaceManifest


def workspace_context_block(manifest: WorkspaceManifest | None) -> str:
    """Return a formatted multi-repo workspace context block for prompt injection.

    Returns an empty string when the manifest is None or contains only a single
    repository (no additional context needed for single-repo workflows).

    For multi-repo workspaces, returns a formatted block describing each
    repository's name, role, and absolute path on disk.

    Args:
        manifest: The WorkspaceManifest describing the cloned repositories,
                  or None if no workspace manifest is available.

    Returns:
        A formatted string block for inclusion in agent prompts, or an empty
        string if not applicable.
    """
    if manifest is None:
        return ""

    repos = manifest.repos
    if len(repos) <= 1:
        return ""

    lines: list[str] = [
        "## Workspace Repositories",
        "",
        "This task spans multiple repositories. Each repository is listed below with its role and local path:",
        "",
    ]

    for repo in repos:
        lines.append(f"- **{repo.repo_name}** (role: {repo.role}): `{repo.absolute_path}`")

    lines.append("")

    return "\n".join(lines)
