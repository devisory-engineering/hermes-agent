"""Bridge dispatcher kanban context into Docker terminal sandboxes.

Host kanban workers receive HERMES_KANBAN_* from the dispatcher, but a
``terminal.backend=docker`` session is a second execution environment that
historically neither forwarded those variables nor mounted the claimed
workspace. This module is the single seam that:

* detects a dispatcher-owned kanban worker on the docker backend
* forwards only the non-secret kanban context vars into the container
* maps the claimed workspace to ``/workspace`` (not the whole board tree)
* rewrites ``HERMES_KANBAN_WORKSPACE`` to the in-sandbox path
* fails closed when the context or workspace cannot be established

Ordinary (non-kanban) Docker sessions are untouched. Secret / provider
forwarding is not widened.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, MutableMapping

logger = logging.getLogger(__name__)

# Exactly the five non-secret context variables the dispatcher injects for a
# claimed card. Do NOT add credentials, tokens, or profile secrets here.
KANBAN_DOCKER_CONTEXT_ENV: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_RUN_ID",
)

# Host-faithful pins forwarded as-is (workspace is rewritten in-sandbox).
_KANBAN_DOCKER_FORWARD_HOST_FAITHFUL: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_RUN_ID",
)

# In-sandbox path for the claimed workspace bind mount.
KANBAN_DOCKER_WORKSPACE_CONTAINER_PATH = "/workspace"


def _is_delegated_child(env: Mapping[str, str]) -> bool:
    if (env.get("HERMES_DELEGATED_CHILD_CONTEXT") or "").strip():
        return True
    try:
        from agent.delegation_context import is_delegated_child_process_context

        # Only consult process context when reading live os.environ.
        if env is os.environ:
            return bool(is_delegated_child_process_context())
    except Exception:
        pass
    return False


def is_kanban_docker_worker(
    env: Mapping[str, str] | None = None,
    *,
    env_type: str | None = None,
) -> bool:
    """True when this process is a kanban worker on the docker terminal backend."""
    src = env if env is not None else os.environ
    if _is_delegated_child(src):
        return False
    if not (src.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    backend = env_type
    if backend is None:
        backend = src.get("TERMINAL_ENV") if env is not None else os.getenv("TERMINAL_ENV", "local")
        if backend is None:
            backend = "local"
    return str(backend or "local").strip().lower() == "docker"


def kanban_workspace_host_path(env: Mapping[str, str] | None = None) -> str:
    """Absolute host path of the claimed workspace, or empty string."""
    src = env if env is not None else os.environ
    raw = (src.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(raw))


def validate_kanban_docker_worker_context(
    *,
    env: Mapping[str, str] | None = None,
    env_type: str | None = None,
) -> None:
    """Fail closed when a docker-backed kanban worker lacks usable context.

    No-op for non-kanban processes and non-docker backends. Raises
    ``RuntimeError`` rather than allowing a hollow worker to stay ``running``.
    """
    src = env if env is not None else os.environ
    if not is_kanban_docker_worker(src, env_type=env_type):
        return

    required = (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    )
    missing = [name for name in required if not (src.get(name) or "").strip()]
    if missing:
        raise RuntimeError(
            "Kanban docker worker missing required context env: "
            + ", ".join(missing)
            + ". Refusing to start a hollow sandbox session."
        )

    host_ws = kanban_workspace_host_path(src)
    if not os.path.isdir(host_ws):
        raise RuntimeError(
            f"Kanban docker worker workspace is not a directory on the host: "
            f"{host_ws!r}. Refusing to start without a usable mount source."
        )


def merge_kanban_docker_forward_env(
    forward_env: list[str] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Append kanban host-faithful context names to docker_forward_env.

    ``HERMES_KANBAN_WORKSPACE`` is intentionally omitted — its in-sandbox
    value is rewritten to ``/workspace`` via ``docker_env``.
    """
    src = env if env is not None else os.environ
    out: list[str] = []
    seen: set[str] = set()
    for item in forward_env or []:
        if not isinstance(item, str):
            continue
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)

    for key in _KANBAN_DOCKER_FORWARD_HOST_FAITHFUL:
        if key in seen:
            continue
        if not (src.get(key) or "").strip():
            # RUN_ID is optional (only set when a run row exists); skip empty.
            continue
        seen.add(key)
        out.append(key)
    return out


def apply_kanban_docker_bridge(
    config: MutableMapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> MutableMapping[str, Any]:
    """Mutate a ``_get_env_config()`` result for docker kanban workers.

    Ordinary docker sessions (no ``HERMES_KANBAN_TASK``) are returned unchanged.
    """
    src = env if env is not None else os.environ
    if str(config.get("env_type") or "").strip().lower() != "docker":
        return config
    if not is_kanban_docker_worker(src, env_type="docker"):
        return config

    validate_kanban_docker_worker_context(env=src, env_type="docker")

    host_ws = kanban_workspace_host_path(src)
    config["host_cwd"] = host_ws
    config["cwd"] = KANBAN_DOCKER_WORKSPACE_CONTAINER_PATH
    config["docker_mount_cwd_to_workspace"] = True
    config["kanban_docker_bridge"] = True
    config["require_workspace_mount"] = True

    forward = list(config.get("docker_forward_env") or [])
    config["docker_forward_env"] = merge_kanban_docker_forward_env(forward, env=src)

    docker_env = dict(config.get("docker_env") or {})
    # Rewrite workspace to the in-container mount path. Host path stays on the
    # host process via os.environ for host-side kanban_* tools.
    docker_env["HERMES_KANBAN_WORKSPACE"] = KANBAN_DOCKER_WORKSPACE_CONTAINER_PATH
    for key in _KANBAN_DOCKER_FORWARD_HOST_FAITHFUL:
        val = (src.get(key) or "").strip()
        if val:
            docker_env.setdefault(key, val)
    config["docker_env"] = docker_env

    logger.info(
        "Kanban docker bridge: forwarding context for task %s; mounting %s -> %s",
        (src.get("HERMES_KANBAN_TASK") or "").strip(),
        host_ws,
        KANBAN_DOCKER_WORKSPACE_CONTAINER_PATH,
    )
    return config


def ensure_kanban_docker_worker_ready(
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Startup gate for docker-backed kanban workers (cli / agent entry).

    Call early on the quiet single-query path so a missing mount source fails
    the worker process instead of leaving a hollow ``running`` claim.
    """
    src = env if env is not None else os.environ
    backend = (src.get("TERMINAL_ENV") if env is not None else os.getenv("TERMINAL_ENV", "local")) or "local"
    if str(backend).strip().lower() != "docker":
        # Also honor config.yaml terminal.backend when env is unset — best effort.
        if env is None and not os.getenv("TERMINAL_ENV"):
            try:
                from hermes_cli.config import load_config

                cfg_backend = ((load_config().get("terminal") or {}).get("backend") or "local")
                if str(cfg_backend).strip().lower() != "docker":
                    return
                backend = "docker"
            except Exception:
                return
        else:
            return
    validate_kanban_docker_worker_context(env=src, env_type=str(backend))
