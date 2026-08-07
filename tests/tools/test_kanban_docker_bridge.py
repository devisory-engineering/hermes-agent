"""Regression: bridge kanban worker context into Docker terminal sandboxes."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.kanban_docker_bridge import (
    KANBAN_DOCKER_CONTEXT_ENV,
    KANBAN_DOCKER_WORKSPACE_CONTAINER_PATH,
    apply_kanban_docker_bridge,
    ensure_kanban_docker_worker_ready,
    merge_kanban_docker_forward_env,
    validate_kanban_docker_worker_context,
)


def _kanban_env(tmp_path: Path, **overrides) -> dict[str, str]:
    ws = tmp_path / "task-ws"
    ws.mkdir()
    env = {
        "HERMES_KANBAN_TASK": "t_abc123",
        "HERMES_KANBAN_WORKSPACE": str(ws),
        "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
        "HERMES_KANBAN_BOARD": "spartans-review",
        "HERMES_KANBAN_RUN_ID": "42",
        "TERMINAL_ENV": "docker",
    }
    env.update(overrides)
    return env


def _base_docker_config(**overrides) -> dict:
    cfg = {
        "env_type": "docker",
        "cwd": "/root",
        "host_cwd": None,
        "docker_mount_cwd_to_workspace": False,
        "docker_forward_env": [],
        "docker_env": {},
        "docker_volumes": [],
    }
    cfg.update(overrides)
    return cfg


def test_context_env_is_exactly_five_non_secret_names():
    assert KANBAN_DOCKER_CONTEXT_ENV == (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_RUN_ID",
    )


def test_ordinary_docker_session_untouched(tmp_path):
    """Non-kanban docker configs must not gain kanban env or mounts."""
    cfg = _base_docker_config(docker_forward_env=["GITHUB_TOKEN"])
    out = apply_kanban_docker_bridge(cfg, env={"TERMINAL_ENV": "docker", "HOME": str(tmp_path)})
    assert out["docker_forward_env"] == ["GITHUB_TOKEN"]
    assert out["host_cwd"] is None
    assert out["docker_mount_cwd_to_workspace"] is False
    assert out["cwd"] == "/root"
    assert "HERMES_KANBAN_TASK" not in (out.get("docker_env") or {})
    assert not out.get("kanban_docker_bridge")
    assert not out.get("require_workspace_mount")


def test_kanban_docker_bridge_forwards_context_and_mounts_workspace(tmp_path):
    env = _kanban_env(tmp_path)
    cfg = _base_docker_config(docker_forward_env=["EXISTING_OPT_IN"])
    out = apply_kanban_docker_bridge(cfg, env=env)

    assert out["kanban_docker_bridge"] is True
    assert out["require_workspace_mount"] is True
    assert out["docker_mount_cwd_to_workspace"] is True
    assert out["cwd"] == KANBAN_DOCKER_WORKSPACE_CONTAINER_PATH
    assert out["host_cwd"] == str(tmp_path / "task-ws")
    # Claimed workspace only — not the board tree root.
    assert out["host_cwd"].endswith("task-ws")
    assert "boards" not in Path(out["host_cwd"]).name

    forward = out["docker_forward_env"]
    assert "EXISTING_OPT_IN" in forward
    assert "HERMES_KANBAN_TASK" in forward
    assert "HERMES_KANBAN_DB" in forward
    assert "HERMES_KANBAN_BOARD" in forward
    assert "HERMES_KANBAN_RUN_ID" in forward
    # Workspace is rewritten via docker_env, not host-path forward.
    assert "HERMES_KANBAN_WORKSPACE" not in forward

    denv = out["docker_env"]
    assert denv["HERMES_KANBAN_WORKSPACE"] == "/workspace"
    assert denv["HERMES_KANBAN_TASK"] == "t_abc123"
    assert denv["HERMES_KANBAN_DB"] == env["HERMES_KANBAN_DB"]
    assert denv["HERMES_KANBAN_BOARD"] == "spartans-review"
    assert denv["HERMES_KANBAN_RUN_ID"] == "42"

    # Must not widen secrets.
    for secretish in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "GITHUB_TOKEN"):
        assert secretish not in forward or secretish == "EXISTING_OPT_IN"


def test_merge_forward_skips_empty_run_id(tmp_path):
    env = _kanban_env(tmp_path)
    env.pop("HERMES_KANBAN_RUN_ID")
    merged = merge_kanban_docker_forward_env([], env=env)
    assert "HERMES_KANBAN_TASK" in merged
    assert "HERMES_KANBAN_RUN_ID" not in merged


def test_fail_closed_missing_task(tmp_path):
    env = _kanban_env(tmp_path)
    env["HERMES_KANBAN_TASK"] = ""
    # Without task id this is not a kanban docker worker — no-op validate.
    validate_kanban_docker_worker_context(env=env, env_type="docker")


def test_fail_closed_missing_workspace_dir(tmp_path):
    env = _kanban_env(tmp_path, HERMES_KANBAN_WORKSPACE=str(tmp_path / "missing-ws"))
    with pytest.raises(RuntimeError, match="not a directory"):
        validate_kanban_docker_worker_context(env=env, env_type="docker")


def test_fail_closed_missing_board(tmp_path):
    env = _kanban_env(tmp_path)
    env["HERMES_KANBAN_BOARD"] = ""
    with pytest.raises(RuntimeError, match="missing required context"):
        validate_kanban_docker_worker_context(env=env, env_type="docker")


def test_apply_bridge_raises_when_workspace_missing(tmp_path):
    env = _kanban_env(tmp_path, HERMES_KANBAN_WORKSPACE=str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="not a directory"):
        apply_kanban_docker_bridge(_base_docker_config(), env=env)


def test_local_backend_kanban_worker_not_bridged(tmp_path):
    env = _kanban_env(tmp_path, TERMINAL_ENV="local")
    cfg = _base_docker_config()
    cfg["env_type"] = "local"
    out = apply_kanban_docker_bridge(cfg, env=env)
    assert out.get("kanban_docker_bridge") is None
    assert out["docker_forward_env"] == []


def test_delegated_child_does_not_bridge(tmp_path):
    env = _kanban_env(tmp_path)
    env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    cfg = _base_docker_config()
    out = apply_kanban_docker_bridge(cfg, env=env)
    assert not out.get("kanban_docker_bridge")
    assert out["host_cwd"] is None


def test_get_env_config_kanban_bridge_integration(tmp_path, monkeypatch):
    """End-to-end through terminal_tool._get_env_config for a kanban worker."""
    import tools.terminal_tool as tt

    ws = tmp_path / "claimed"
    ws.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_live01")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kb.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "program")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.delenv("TERMINAL_DOCKER_FORWARD_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    cfg = tt._get_env_config()
    assert cfg["env_type"] == "docker"
    assert cfg["cwd"] == "/workspace"
    assert cfg["host_cwd"] == str(ws)
    assert cfg["docker_mount_cwd_to_workspace"] is True
    assert "HERMES_KANBAN_TASK" in cfg["docker_forward_env"]
    assert cfg["docker_env"]["HERMES_KANBAN_WORKSPACE"] == "/workspace"
    assert cfg["docker_env"]["HERMES_KANBAN_TASK"] == "t_live01"
    assert cfg.get("require_workspace_mount") is True


def test_get_env_config_ordinary_docker_no_kanban(tmp_path, monkeypatch):
    import tools.terminal_tool as tt

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    for key in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_RUN_ID",
        "TERMINAL_CWD",
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
        "TERMINAL_DOCKER_FORWARD_ENV",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = tt._get_env_config()
    assert cfg["env_type"] == "docker"
    assert cfg["host_cwd"] is None
    assert cfg["docker_mount_cwd_to_workspace"] is False
    assert "HERMES_KANBAN_TASK" not in (cfg.get("docker_forward_env") or [])
    assert "HERMES_KANBAN_TASK" not in (cfg.get("docker_env") or {})


def test_docker_env_require_workspace_mount_fails_closed(monkeypatch, tmp_path):
    from tools.environments import docker as docker_env

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    docker_env._cgroup_limits_ok = True

    def _run(cmd, **kwargs):
        import subprocess
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "version":
            return subprocess.CompletedProcess(cmd, 0, stdout="Docker version", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)

    missing = tmp_path / "gone"
    with pytest.raises(RuntimeError, match="requires workspace mount"):
        docker_env.DockerEnvironment(
            image="python:3.11",
            cwd="/workspace",
            host_cwd=str(missing),
            auto_mount_cwd=True,
            require_workspace_mount=True,
            task_id="t",
        )


def test_docker_env_kanban_mount_adds_volume(monkeypatch, tmp_path):
    from tools.environments import docker as docker_env
    import subprocess

    project = tmp_path / "ws"
    project.mkdir()
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    docker_env._cgroup_limits_ok = True
    calls = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, list) else cmd)
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "version":
            return subprocess.CompletedProcess(cmd, 0, stdout="Docker version", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        host_cwd=str(project),
        auto_mount_cwd=True,
        require_workspace_mount=True,
        task_id="t",
        env={"HERMES_KANBAN_TASK": "t_x", "HERMES_KANBAN_WORKSPACE": "/workspace"},
        forward_env=["HERMES_KANBAN_TASK"],
    )
    run_calls = [c for c in calls if isinstance(c, list) and len(c) >= 2 and c[1] == "run"]
    assert run_calls
    joined = " ".join(run_calls[0])
    assert f"{project}:/workspace" in joined


def test_ensure_ready_no_op_for_local(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    ensure_kanban_docker_worker_ready()


def test_ensure_ready_fails_for_docker_missing_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "no"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "b")
    with pytest.raises(RuntimeError, match="not a directory"):
        ensure_kanban_docker_worker_ready()
