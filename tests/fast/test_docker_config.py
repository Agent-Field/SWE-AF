"""Tests for swe-fast docker-compose and pyproject.toml configuration."""

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent.parent


def load_docker_compose():
    with open(REPO_ROOT / "docker-compose.yml") as f:
        return yaml.safe_load(f)


def load_pyproject():
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_swe_fast_service_exists():
    """swe-fast service is present in docker-compose.yml."""
    compose = load_docker_compose()
    assert "swe-fast" in compose["services"], "swe-fast service must exist in docker-compose.yml"


def test_swe_fast_node_id_env_var():
    """swe-fast service has NODE_ID=swe-fast in environment."""
    compose = load_docker_compose()
    env = compose["services"]["swe-fast"]["environment"]
    assert "NODE_ID=swe-fast" in env, "NODE_ID=swe-fast must be in swe-fast environment"


def test_swe_fast_port_env_var():
    """swe-fast service has PORT=8004 in environment."""
    compose = load_docker_compose()
    env = compose["services"]["swe-fast"]["environment"]
    assert "PORT=8004" in env, "PORT=8004 must be in swe-fast environment"


def test_swe_fast_port_mapping():
    """swe-fast service exposes port 8004:8004."""
    compose = load_docker_compose()
    ports = compose["services"]["swe-fast"]["ports"]
    assert "8004:8004" in ports, "Port mapping 8004:8004 must be defined for swe-fast"


def test_swe_fast_depends_on_control_plane():
    """swe-fast service depends_on control-plane."""
    compose = load_docker_compose()
    depends_on = compose["services"]["swe-fast"]["depends_on"]
    assert "control-plane" in depends_on, "swe-fast must depend_on control-plane"


def test_swe_fast_agentfield_server_env_var():
    """swe-fast service has AGENTFIELD_SERVER=http://control-plane:8080 in environment."""
    compose = load_docker_compose()
    env = compose["services"]["swe-fast"]["environment"]
    assert "AGENTFIELD_SERVER=http://control-plane:8080" in env, (
        "AGENTFIELD_SERVER=http://control-plane:8080 must be in swe-fast environment"
    )


def test_swe_agent_service_unchanged():
    """Existing swe-agent service is present and unchanged."""
    compose = load_docker_compose()
    assert "swe-agent" in compose["services"], "swe-agent service must still exist"
    swe_agent = compose["services"]["swe-agent"]
    env = swe_agent["environment"]
    assert "NODE_ID=swe-planner" in env, "swe-agent NODE_ID must remain swe-planner"
    assert "PORT=8003" in env, "swe-agent PORT must remain 8003"
    assert "8003:8003" in swe_agent["ports"], "swe-agent port mapping must remain 8003:8003"


def test_pyproject_swe_fast_script():
    """pyproject.toml [project.scripts] contains swe-fast = 'swe_af.fast.app:main'."""
    pyproject = load_pyproject()
    scripts = pyproject["project"]["scripts"]
    assert "swe-fast" in scripts, "swe-fast must be in [project.scripts]"
    assert scripts["swe-fast"] == "swe_af.fast.app:main", (
        "swe-fast script must point to swe_af.fast.app:main"
    )


def test_pyproject_swe_af_script_unchanged():
    """Existing swe-af entry in pyproject.toml [project.scripts] is unchanged."""
    pyproject = load_pyproject()
    scripts = pyproject["project"]["scripts"]
    assert "swe-af" in scripts, "swe-af must still be in [project.scripts]"
    assert scripts["swe-af"] == "swe_af.app:main", (
        "swe-af script must still point to swe_af.app:main"
    )
