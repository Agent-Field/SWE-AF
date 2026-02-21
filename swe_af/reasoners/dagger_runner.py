"""Dagger CI/CD integration for agents.

This module provides Dagger-based CI/CD pipelines that agents can invoke
to validate their work in isolated, reproducible containers.

Key capabilities:
- Run tests in clean containers (no host pollution)
- Build projects with layer caching
- Spin up services (postgres, redis) for integration tests
- Detect project type and auto-configure pipelines

Usage from agents:
    result = await call_fn("run_dagger_pipeline",
        repo_path=worktree_path,
        pipeline="test",
        services=["postgres"]
    )
"""

from __future__ import annotations

import os
from enum import Enum

import dagger
from pydantic import BaseModel, ConfigDict

from . import router


def _note(message: str, tags: list[str] | None = None) -> None:
    """Log to router if attached, otherwise print to stdout."""
    try:
        router.note(message, tags=tags or [])
    except RuntimeError:
        print(f"[dagger] {message}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DaggerPipelineType(str, Enum):
    """Available Dagger pipeline types."""

    TEST = "test"  # Run test suite
    BUILD = "build"  # Build the project
    LINT = "lint"  # Run linters/formatters
    FULL = "full"  # Full CI: build + test + lint
    CUSTOM = "custom"  # Custom command


class DaggerService(str, Enum):
    """Available services to spin up."""

    POSTGRES = "postgres"
    REDIS = "redis"
    MYSQL = "mysql"
    MONGODB = "mongodb"


class DaggerTestResult(BaseModel):
    """Result of running tests via Dagger."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    output: str = ""
    errors: str = ""
    test_failures: list[dict[str, str]] = []  # [{test_name, file, error}]


class DaggerBuildResult(BaseModel):
    """Result of building via Dagger."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    output: str = ""
    errors: str = ""
    artifacts: list[str] = []  # Built artifact paths


class DaggerLintResult(BaseModel):
    """Result of linting via Dagger."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    issues: list[dict[str, str]] = []  # [{file, line, severity, message}]
    output: str = ""
    errors: str = ""


class DaggerPipelineResult(BaseModel):
    """Combined result of a Dagger pipeline run."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    pipeline: str
    test_result: DaggerTestResult | None = None
    build_result: DaggerBuildResult | None = None
    lint_result: DaggerLintResult | None = None
    summary: str = ""
    duration_seconds: float = 0.0


class ProjectDetectionResult(BaseModel):
    """Result of project type detection."""

    model_config = ConfigDict(extra="forbid")

    language: str  # python, node, go, rust, etc.
    framework: str = ""  # django, fastapi, react, nextjs, etc.
    build_system: str = ""  # poetry, npm, cargo, etc.
    test_command: str = ""
    lint_command: str = ""
    build_command: str = ""
    detected_files: list[str] = []


# ---------------------------------------------------------------------------
# Project Detection
# ---------------------------------------------------------------------------


async def detect_project_type(source_dir: str) -> ProjectDetectionResult:
    """Detect project type by examining files in the directory.

    Returns a ProjectDetectionResult with detected language, framework,
    and appropriate commands for testing, linting, and building.
    """
    detected_files = []

    # Check for common project files
    file_checks = [
        ("pyproject.toml", "python"),
        ("setup.py", "python"),
        ("requirements.txt", "python"),
        ("package.json", "node"),
        ("Cargo.toml", "rust"),
        ("go.mod", "go"),
        ("pom.xml", "java"),
        ("build.gradle", "java"),
        ("Gemfile", "ruby"),
    ]

    for filename, lang in file_checks:
        path = os.path.join(source_dir, filename)
        if os.path.exists(path):
            detected_files.append(filename)

    # Python detection
    if any(
        f in detected_files for f in ["pyproject.toml", "setup.py", "requirements.txt"]
    ):
        framework = ""
        test_cmd = "pytest -v"
        lint_cmd = "ruff check ."
        build_cmd = "pip install -e ."

        # Check for pytest
        pyproject_path = os.path.join(source_dir, "pyproject.toml")

        if os.path.exists(pyproject_path):
            try:
                with open(pyproject_path, "r") as f:
                    content = f.read()
                    if "fastapi" in content.lower():
                        framework = "fastapi"
                    elif "django" in content.lower():
                        framework = "django"
                        test_cmd = "python manage.py test"
                    elif "flask" in content.lower():
                        framework = "flask"
                    if "poetry" in content.lower():
                        build_cmd = "poetry install"
            except Exception:
                pass

        return ProjectDetectionResult(
            language="python",
            framework=framework,
            build_system="poetry" if "poetry" in build_cmd else "pip",
            test_command=test_cmd,
            lint_command=lint_cmd,
            build_command=build_cmd,
            detected_files=detected_files,
        )

    # Node detection
    if "package.json" in detected_files:
        framework = ""
        test_cmd = "npm test"
        lint_cmd = "npm run lint"
        build_cmd = "npm run build"

        package_json_path = os.path.join(source_dir, "package.json")
        try:
            import json

            with open(package_json_path, "r") as f:
                pkg = json.load(f)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "next" in deps:
                    framework = "nextjs"
                elif "react" in deps:
                    framework = "react"
                elif "express" in deps:
                    framework = "express"
                if pkg.get("scripts", {}).get("test"):
                    test_cmd = "npm test"
                if pkg.get("scripts", {}).get("lint"):
                    lint_cmd = "npm run lint"
                if pkg.get("scripts", {}).get("build"):
                    build_cmd = "npm run build"
        except Exception:
            pass

        return ProjectDetectionResult(
            language="node",
            framework=framework,
            build_system="npm",
            test_command=test_cmd,
            lint_command=lint_cmd,
            build_command=build_cmd,
            detected_files=detected_files,
        )

    # Rust detection
    if "Cargo.toml" in detected_files:
        return ProjectDetectionResult(
            language="rust",
            build_system="cargo",
            test_command="cargo test",
            lint_command="cargo clippy",
            build_command="cargo build --release",
            detected_files=detected_files,
        )

    # Go detection
    if "go.mod" in detected_files:
        return ProjectDetectionResult(
            language="go",
            build_system="go",
            test_command="go test ./...",
            lint_command="golangci-lint run",
            build_command="go build ./...",
            detected_files=detected_files,
        )

    # Fallback
    return ProjectDetectionResult(
        language="unknown",
        detected_files=detected_files,
    )


# ---------------------------------------------------------------------------
# Service Containers
# ---------------------------------------------------------------------------


def get_service_container(client: dagger.Client, service: str) -> dagger.Container:
    """Get a service container for use in pipelines."""
    if service == DaggerService.POSTGRES.value:
        return (
            client.container()
            .from_("postgres:16-alpine")
            .with_env_variable("POSTGRES_USER", "test")
            .with_env_variable("POSTGRES_PASSWORD", "test")
            .with_env_variable("POSTGRES_DB", "test")
            .with_exposed_port(5432)
        )
    elif service == DaggerService.REDIS.value:
        return client.container().from_("redis:7-alpine").with_exposed_port(6379)
    elif service == DaggerService.MYSQL.value:
        return (
            client.container()
            .from_("mysql:8")
            .with_env_variable("MYSQL_ROOT_PASSWORD", "test")
            .with_env_variable("MYSQL_DATABASE", "test")
            .with_exposed_port(3306)
        )
    elif service == DaggerService.MONGODB.value:
        return (
            client.container()
            .from_("mongo:7")
            .with_env_variable("MONGO_INITDB_ROOT_USERNAME", "test")
            .with_env_variable("MONGO_INITDB_ROOT_PASSWORD", "test")
            .with_exposed_port(27017)
        )
    else:
        raise ValueError(f"Unknown service: {service}")


# ---------------------------------------------------------------------------
# Pipeline Builders
# ---------------------------------------------------------------------------


def build_python_pipeline(
    client: dagger.Client,
    source: dagger.Directory,
    project: ProjectDetectionResult,
    pipeline: str,
    services: list[str],
) -> dagger.Container:
    """Build a Python CI pipeline.

    Optimizes dependency installation based on pipeline type:
    - lint: Only installs ruff (fast, ~5s)
    - build: Only installs build deps
    - test: Full dependencies
    - full: Full dependencies
    """
    container = (
        client.container()
        .from_("python:3.12-slim")
        .with_mounted_directory("/app", source)
        .with_workdir("/app")
    )

    pipeline_type = DaggerPipelineType(pipeline)

    # Fast path for lint - only need ruff, not all deps
    if pipeline_type == DaggerPipelineType.LINT:
        container = container.with_exec(["pip", "install", "--quiet", "ruff"])
        # Add services if needed
        for svc in services:
            service_container = get_service_container(client, svc)
            if svc == DaggerService.POSTGRES.value:
                container = container.with_service_binding(
                    "postgres", service_container.as_service()
                )
                container = container.with_env_variable(
                    "DATABASE_URL", "postgresql://test:test@postgres:5432/test"
                )
            elif svc == DaggerService.REDIS.value:
                container = container.with_service_binding(
                    "redis", service_container.as_service()
                )
                container = container.with_env_variable(
                    "REDIS_URL", "redis://redis:6379"
                )
        return container

    # Install dependencies based on build system
    if project.build_system == "poetry":
        container = container.with_exec(["pip", "install", "poetry"]).with_exec(
            ["poetry", "install", "--no-interaction"]
        )
        container = container.with_env_variable(
            "PATH",
            "/root/.cache/pypoetry/virtualenvs/*/bin:/usr/local/bin:/usr/bin:/bin",
        )
    else:
        container = container.with_exec(
            ["pip", "install", "--quiet", "--upgrade", "pip"]
        ).with_exec(["pip", "install", "--quiet", "-e", ".[dev]"])

    # Add services
    for svc in services:
        service_container = get_service_container(client, svc)
        # Start service and get endpoint
        if svc == DaggerService.POSTGRES.value:
            container = container.with_service_binding(
                "postgres", service_container.as_service()
            )
            container = container.with_env_variable(
                "DATABASE_URL", "postgresql://test:test@postgres:5432/test"
            )
        elif svc == DaggerService.REDIS.value:
            container = container.with_service_binding(
                "redis", service_container.as_service()
            )
            container = container.with_env_variable("REDIS_URL", "redis://redis:6379")

    return container


def build_node_pipeline(
    client: dagger.Client,
    source: dagger.Directory,
    project: ProjectDetectionResult,
    pipeline: str,
    services: list[str],
) -> dagger.Container:
    """Build a Node.js CI pipeline."""
    container = (
        client.container()
        .from_("node:20-slim")
        .with_mounted_directory("/app", source)
        .with_workdir("/app")
        .with_exec(["npm", "ci"])  # Clean install
    )

    # Add services
    for svc in services:
        service_container = get_service_container(client, svc)
        if svc == DaggerService.POSTGRES.value:
            container = container.with_service_binding(
                "postgres", service_container.as_service()
            )
            container = container.with_env_variable(
                "DATABASE_URL", "postgresql://test:test@postgres:5432/test"
            )
        elif svc == DaggerService.REDIS.value:
            container = container.with_service_binding(
                "redis", service_container.as_service()
            )
            container = container.with_env_variable("REDIS_URL", "redis://redis:6379")

    return container


# ---------------------------------------------------------------------------
# Reasoners
# ---------------------------------------------------------------------------


@router.reasoner()
async def run_dagger_pipeline(
    repo_path: str,
    pipeline: str = "test",
    services: list[str] | None = None,
    custom_command: str = "",
    timeout_seconds: int = 600,
    model: str = "haiku",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run a CI/CD pipeline via Dagger in an isolated container.

    This is the main entry point for agents to validate their work.
    Automatically detects project type and runs appropriate commands.

    Args:
        repo_path: Path to the repository/worktree to test
        pipeline: Type of pipeline - "test", "build", "lint", "full", or "custom"
        services: List of services to spin up - "postgres", "redis", "mysql", "mongodb"
        custom_command: Custom command to run (only used when pipeline="custom")
        timeout_seconds: Maximum time for the pipeline to run
        model: Model for any AI-assisted parsing (rarely needed)
        permission_mode: Permission mode for any AI calls
        ai_provider: AI provider for any AI-assisted parsing

    Returns:
        DaggerPipelineResult with success status and detailed results

    Example:
        result = await call_fn("run_dagger_pipeline",
            repo_path="/path/to/worktree",
            pipeline="test",
            services=["postgres"]
        )
        if not result["success"]:
            print(f"Tests failed: {result['test_result']['errors']}")
    """
    import time

    start_time = time.time()

    services = services or []
    pipeline_type = DaggerPipelineType(pipeline)

    _note(
        f"Dagger pipeline starting: {pipeline} for {repo_path}",
        tags=["dagger", "pipeline", "start"],
    )

    # Detect project type
    project = await detect_project_type(repo_path)
    _note(
        f"Detected project: {project.language}/{project.framework or 'vanilla'}",
        tags=["dagger", "detect"],
    )

    try:
        async with dagger.Connection(
            dagger.Config(timeout=120, execute_timeout=None)
        ) as client:
            # Mount the source directory
            source = client.host().directory(repo_path)

            # Build appropriate container based on language
            if project.language == "python":
                container = build_python_pipeline(
                    client, source, project, pipeline, services
                )
            elif project.language == "node":
                container = build_node_pipeline(
                    client, source, project, pipeline, services
                )
            else:
                # Generic fallback - use Python with manual setup
                container = build_python_pipeline(
                    client, source, project, pipeline, services
                )

            # Run the requested pipeline
            test_result = None
            build_result = None
            lint_result = None

            # Execute pipeline stages
            if pipeline_type == DaggerPipelineType.TEST:
                result_container = container.with_exec(
                    ["sh", "-c", project.test_command],
                    experimental_privileged_nesting=True,
                )
                try:
                    output = await result_container.stdout()
                    errors = await result_container.stderr()
                    exit_code = 0
                except dagger.ExecError as e:
                    output = e.stdout or ""
                    errors = e.stderr or str(e)
                    exit_code = 1

                test_result = _parse_test_output(
                    project.language, output, errors, exit_code
                )

            elif pipeline_type == DaggerPipelineType.BUILD:
                result_container = container.with_exec(
                    ["sh", "-c", project.build_command],
                    experimental_privileged_nesting=True,
                )
                try:
                    output = await result_container.stdout()
                    errors = await result_container.stderr()
                    exit_code = 0
                except dagger.ExecError as e:
                    output = e.stdout or ""
                    errors = e.stderr or str(e)
                    exit_code = 1

                build_result = DaggerBuildResult(
                    success=exit_code == 0,
                    output=output,
                    errors=errors,
                )

            elif pipeline_type == DaggerPipelineType.LINT:
                result_container = container.with_exec(
                    ["sh", "-c", project.lint_command],
                    experimental_privileged_nesting=True,
                )
                try:
                    output = await result_container.stdout()
                    errors = await result_container.stderr()
                    exit_code = 0
                except dagger.ExecError as e:
                    output = e.stdout or ""
                    errors = e.stderr or str(e)
                    exit_code = 1

                lint_result = _parse_lint_output(
                    project.language, output, errors, exit_code
                )

            elif pipeline_type == DaggerPipelineType.FULL:
                # Run all stages
                success = True

                # Build
                try:
                    build_out = await container.with_exec(
                        ["sh", "-c", project.build_command],
                    ).stdout()
                    build_result = DaggerBuildResult(success=True, output=build_out)
                except dagger.ExecError as e:
                    build_result = DaggerBuildResult(
                        success=False,
                        output=e.stdout or "",
                        errors=e.stderr or str(e),
                    )
                    success = False

                # Test (only if build succeeded)
                if build_result.success:
                    try:
                        test_out = await container.with_exec(
                            ["sh", "-c", project.test_command],
                        ).stdout()
                        test_result = _parse_test_output(
                            project.language, test_out, "", 0
                        )
                    except dagger.ExecError as e:
                        test_result = _parse_test_output(
                            project.language, e.stdout or "", e.stderr or str(e), 1
                        )
                        success = False

                # Lint (non-blocking for full pipeline)
                try:
                    lint_out = await container.with_exec(
                        ["sh", "-c", project.lint_command],
                    ).stdout()
                    lint_result = _parse_lint_output(project.language, lint_out, "", 0)
                except dagger.ExecError as e:
                    lint_result = _parse_lint_output(
                        project.language, e.stdout or "", e.stderr or str(e), 1
                    )

            elif pipeline_type == DaggerPipelineType.CUSTOM:
                if not custom_command:
                    return DaggerPipelineResult(
                        success=False,
                        pipeline=pipeline,
                        summary="Custom pipeline requires custom_command parameter",
                    ).model_dump()

                result_container = container.with_exec(
                    ["sh", "-c", custom_command],
                    experimental_privileged_nesting=True,
                )
                try:
                    output = await result_container.stdout()
                    errors = await result_container.stderr()
                    exit_code = 0
                except dagger.ExecError as e:
                    output = e.stdout or ""
                    errors = e.stderr or str(e)
                    exit_code = 1

                # Treat custom as a test for result purposes
                test_result = DaggerTestResult(
                    success=exit_code == 0,
                    output=output,
                    errors=errors,
                )

            # Build final result
            duration = time.time() - start_time

            # Determine overall success
            success = True
            if test_result and not test_result.success:
                success = False
            if build_result and not build_result.success:
                success = False
            # Lint failures are non-blocking

            summary = _build_summary(
                pipeline, test_result, build_result, lint_result, success
            )

            _note(
                f"Dagger pipeline complete: {pipeline} - success={success}",
                tags=["dagger", "pipeline", "complete"],
            )

            return DaggerPipelineResult(
                success=success,
                pipeline=pipeline,
                test_result=test_result,
                build_result=build_result,
                lint_result=lint_result,
                summary=summary,
                duration_seconds=round(duration, 2),
            ).model_dump()

    except Exception as e:
        _note(
            f"Dagger pipeline failed: {e}",
            tags=["dagger", "pipeline", "error"],
        )
        return DaggerPipelineResult(
            success=False,
            pipeline=pipeline,
            summary=f"Dagger pipeline error: {e}",
        ).model_dump()


@router.reasoner()
async def run_dagger_test(
    repo_path: str,
    services: list[str] | None = None,
    test_command: str = "",
    model: str = "haiku",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Convenience wrapper for running just tests via Dagger.

    Simpler interface for the common case of just running tests.
    Auto-detects test command if not provided.

    Args:
        repo_path: Path to the repository
        services: Optional services to spin up
        test_command: Override auto-detected test command

    Returns:
        DaggerPipelineResult focused on test results
    """
    return await run_dagger_pipeline(
        repo_path=repo_path,
        pipeline="custom" if test_command else "test",
        services=services,
        custom_command=test_command,
        model=model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    )


@router.reasoner()
async def detect_project(
    repo_path: str,
    model: str = "haiku",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Detect project type and return configuration.

    Useful for agents to understand what kind of project they're working on.

    Args:
        repo_path: Path to the repository

    Returns:
        ProjectDetectionResult with language, framework, commands, etc.
    """
    _note(
        f"Detecting project type: {repo_path}",
        tags=["dagger", "detect"],
    )

    result = await detect_project_type(repo_path)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_test_output(
    language: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> DaggerTestResult:
    """Parse test output to extract structured results."""
    output = stdout + "\n" + stderr

    # Python/pytest parsing
    if language == "python":
        return _parse_pytest_output(output, exit_code)

    # Node/jest parsing
    if language == "node":
        return _parse_jest_output(output, exit_code)

    # Generic fallback
    return DaggerTestResult(
        success=exit_code == 0,
        output=stdout,
        errors=stderr,
    )


def _parse_pytest_output(output: str, exit_code: int) -> DaggerTestResult:
    """Parse pytest output for test results."""
    import re

    # Match pytest summary line: "X passed, Y failed, Z skipped"
    summary_pattern = r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?"
    match = re.search(summary_pattern, output)

    if match:
        passed = int(match.group(1))
        failed = int(match.group(2) or 0)

        # Extract failure details
        failures = []
        failure_pattern = r"FAILED ([^\s]+)::([^\s]+)"
        for fail_match in re.finditer(failure_pattern, output):
            failures.append(
                {
                    "file": fail_match.group(1),
                    "test_name": fail_match.group(2),
                    "error": "",
                }
            )

        return DaggerTestResult(
            success=exit_code == 0,
            tests_run=passed + failed,
            tests_passed=passed,
            tests_failed=failed,
            output=output,
            test_failures=failures,
        )

    return DaggerTestResult(
        success=exit_code == 0,
        output=output,
    )


def _parse_jest_output(output: str, exit_code: int) -> DaggerTestResult:
    """Parse Jest output for test results."""
    import re

    # Match jest summary: "Tests: X passed, Y failed, Z total"
    summary_pattern = (
        r"Tests:\s+(\d+) (?:passed|failed)(?:, (\d+) failed)?(?:, (\d+) total)?"
    )
    match = re.search(summary_pattern, output)

    if match:
        passed_or_failed = int(match.group(1))
        failed = int(match.group(2) or 0)

        # Determine if first number was passed or failed
        if "passed" in match.group(0):
            passed = passed_or_failed
        else:
            passed = 0
            failed = passed_or_failed

        return DaggerTestResult(
            success=exit_code == 0,
            tests_run=passed + failed,
            tests_passed=passed,
            tests_failed=failed,
            output=output,
        )

    return DaggerTestResult(
        success=exit_code == 0,
        output=output,
    )


def _parse_lint_output(
    language: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> DaggerLintResult:
    """Parse linter output to extract issues."""
    output = stdout + "\n" + stderr
    issues = []

    # Ruff format: "file:line:col: CODE message"
    if language == "python":
        import re

        pattern = r"([^\s:]+):(\d+):(\d+): ([A-Z]+\d+) (.+)"
        for match in re.finditer(pattern, output):
            issues.append(
                {
                    "file": match.group(1),
                    "line": match.group(2),
                    "severity": "error" if "E" in match.group(4) else "warning",
                    "message": f"{match.group(4)}: {match.group(5)}",
                }
            )

    return DaggerLintResult(
        success=exit_code == 0,
        issues=issues,
        output=stdout,
        errors=stderr,
    )


def _build_summary(
    pipeline: str,
    test_result: DaggerTestResult | None,
    build_result: DaggerBuildResult | None,
    lint_result: DaggerLintResult | None,
    success: bool,
) -> str:
    """Build a human-readable summary of the pipeline results."""
    parts = [f"Pipeline '{pipeline}' {'succeeded' if success else 'failed'}."]

    if test_result:
        if test_result.tests_run > 0:
            parts.append(
                f"Tests: {test_result.tests_passed}/{test_result.tests_run} passed"
            )
        elif not test_result.success:
            parts.append("Tests failed")
        else:
            parts.append("Tests passed")

    if build_result:
        parts.append(f"Build: {'succeeded' if build_result.success else 'failed'}")

    if lint_result:
        issue_count = len(lint_result.issues)
        if issue_count > 0:
            parts.append(f"Lint: {issue_count} issues found")
        else:
            parts.append("Lint: clean")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Direct-callable helpers (for testing without server)
# ---------------------------------------------------------------------------


async def run_pipeline_direct(
    repo_path: str,
    pipeline: str = "lint",
    services: list[str] | None = None,
    timeout_seconds: int = 120,
) -> dict:
    """Run a Dagger pipeline directly without requiring AgentField server.

    This is a convenience function for testing and standalone usage.
    For agent usage, prefer run_dagger_pipeline via app.call().

    Args:
        repo_path: Path to the repository
        pipeline: "test", "build", "lint", or "full"
        services: Optional services like ["postgres", "redis"]
        timeout_seconds: Timeout for the pipeline

    Returns:
        Dict with success, pipeline, summary, and result details
    """
    import time

    start_time = time.time()
    services = services or []
    pipeline_type = DaggerPipelineType(pipeline)

    # Detect project type
    project = await detect_project_type(repo_path)

    try:
        async with dagger.Connection(
            dagger.Config(timeout=120, execute_timeout=None)
        ) as client:
            source = client.host().directory(repo_path)

            # Build container based on language
            if project.language == "python":
                container = build_python_pipeline(
                    client, source, project, pipeline, services
                )
            elif project.language == "node":
                container = build_node_pipeline(
                    client, source, project, pipeline, services
                )
            else:
                container = build_python_pipeline(
                    client, source, project, pipeline, services
                )

            # Execute pipeline
            test_result = None
            lint_result = None
            success = True

            if pipeline_type == DaggerPipelineType.LINT:
                try:
                    output = await container.with_exec(
                        ["sh", "-c", project.lint_command]
                    ).stdout()
                    lint_result = _parse_lint_output(project.language, output, "", 0)
                except dagger.ExecError as e:
                    lint_result = _parse_lint_output(
                        project.language, e.stdout or "", e.stderr or str(e), 1
                    )

            elif pipeline_type == DaggerPipelineType.TEST:
                try:
                    output = await container.with_exec(
                        ["sh", "-c", project.test_command],
                        experimental_privileged_nesting=True,
                    ).stdout()
                    test_result = _parse_test_output(project.language, output, "", 0)
                except dagger.ExecError as e:
                    test_result = _parse_test_output(
                        project.language, e.stdout or "", e.stderr or str(e), 1
                    )
                    success = False

            duration = time.time() - start_time
            summary = _build_summary(pipeline, test_result, None, lint_result, success)

            return DaggerPipelineResult(
                success=success,
                pipeline=pipeline,
                test_result=test_result,
                lint_result=lint_result,
                summary=summary,
                duration_seconds=round(duration, 2),
            ).model_dump()

    except Exception as e:
        return DaggerPipelineResult(
            success=False,
            pipeline=pipeline,
            summary=f"Pipeline failed: {e}",
            duration_seconds=round(time.time() - start_time, 2),
        ).model_dump()
