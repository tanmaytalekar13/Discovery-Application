"""Unit coverage for OOM (exit 137) diagnosis in the local MCP client.

No Docker required: docker/inspect calls are stubbed at the subprocess
boundary and container_id is faked.
"""
import asyncio
import subprocess

import app.sandbox.local_mcp_client as lm
from app.sandbox.local_mcp_client import LocalConnectResult, LocalMCPClient


def _client(container_id: str | None = "abc123") -> LocalMCPClient:
    client = LocalMCPClient()
    client._container_id = container_id
    return client


def test_exit_137_with_empty_stderr_is_classified_as_oom_without_docker():
    # The observed failure: install completes, no traceback, SIGKILL -> 137.
    client = _client(container_id=None)
    message, reason, variables = client._analyze_error("", 137)

    assert reason is None
    assert variables == []
    assert message is not None
    assert "memory" in message.lower()
    assert "137" in message


def test_exit_137_with_kernel_oom_marker_is_classified_as_oom():
    message, reason, _ = _client()._analyze_error(
        "python: Fatal Python error: killed by signal 9\nKilled", 137
    )

    assert reason is None
    assert message is not None
    assert "memory" in message.lower()


def test_exit_137_none_returncode_requires_docker_evidence():
    # returncode=None (not yet reaped) must NOT be classified from stderr
    # alone; only docker inspect / cgroup counters may confirm OOM.
    assert _client()._detect_oom_kill(None, "") is False


def test_bare_137_without_evidence_is_still_classified_as_oom(monkeypatch):
    # Policy: in the memory-capped sandbox a bare SIGKILL (137) is the OOM
    # killer for practical purposes — nothing else SIGKILLs the server, and
    # the evidence checks can fail (container already torn down, docker
    # hiccup), which is exactly the observed failure mode.
    def no_docker(*_args, **_kwargs):
        raise OSError("docker unavailable / container gone")

    monkeypatch.setattr(subprocess, "run", no_docker)
    assert _client()._detect_oom_kill(137, "") is True


def test_other_exit_codes_are_never_oom():
    assert _client()._detect_oom_kill(1, "out of memory") is False
    assert _client()._detect_oom_kill(127, "") is False


def test_stderr_oom_marker_short_circuits_before_docker_calls(monkeypatch):
    def boom(*_args, **_kwargs):  # must never be reached
        raise AssertionError("docker must not be called when stderr already proves OOM")

    monkeypatch.setattr(subprocess, "run", boom)
    assert _client()._detect_oom_kill(137, "Out of memory") is True


def test_docker_inspect_oomkilled_confirms_oom(monkeypatch):
    def fake_run(cmd, **_kwargs):
        if "inspect" in cmd:
            result = subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        else:
            result = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _client()._detect_oom_kill(137, "") is True


def test_cgroup_oom_kill_counter_confirms_oom(monkeypatch):
    """SIGKILLed `docker exec` children are missed by .State.OOMKilled but
    recorded in the cgroup memory.events oom_kill counter."""
    def fake_run(cmd, **_kwargs):
        if "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        # docker exec cat memory.events
        return subprocess.CompletedProcess(
            cmd, 0, stdout="anon 10\nfile 0\noom_kill 1\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _client()._detect_oom_kill(137, "") is True


def test_none_returncode_with_no_evidence_means_not_oom(monkeypatch):
    # With returncode=None (EOF race) bare-137 fallback must not fire;
    # only positive docker/cgroup evidence may classify as OOM.
    def fake_run(cmd, **_kwargs):
        if "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="anon 10\nfile 0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _client()._detect_oom_kill(None, "") is False


def test_docker_failures_do_not_crash_detection(monkeypatch):
    def fake_run(_cmd, **_kwargs):
        raise OSError("docker not available")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # No crash, falls through to bare-137 classification.
    assert _client()._detect_oom_kill(137, "") is True


def test_oom_kill_is_diagnosed_before_auth_markers():
    # A stderr mentioning credentials must not hijack an OOM kill.
    message, reason, _ = _client()._analyze_error(
        "Error: NOTION_API_KEY is required", 137
    )

    assert reason is None
    assert message is not None
    assert "memory" in message.lower()


# ---------------------------------------------------------------------------
# Container manager: uv cache volume + swap headroom
# ---------------------------------------------------------------------------

def test_pip_command_gains_uv_cache_volume(monkeypatch):
    import app.sandbox.container_manager as cm

    captured: dict = {}

    def fake_containers_run(**config):
        captured.update(config)
        return object()  # sentinel container

    docker = type("DockerStub", (), {})()
    docker.containers = type(
        "ContainersStub", (), {"run": staticmethod(fake_containers_run)}
    )()
    docker.images = type(
        "ImagesStub",
        (),
        {"get": staticmethod(lambda _image: (_ for _ in ()).throw(Exception("not found")))},
    )()
    docker.networks = type(
        "NetworksStub", (), {"list": staticmethod(lambda names=None: [])}
    )()

    manager = cm.ContainerManager.__new__(cm.ContainerManager)
    manager._docker_client = docker

    manager._create_docker_container(
        session_id="s1",
        image="python:3.12-slim",
        command=["sh", "-c", "uvx some-package"],
        env_vars={},
        allowed_domains=[],
        timeout=60,
        registry_type="pip",
    )

    volumes = captured.get("volumes") or {}
    binds = {v["bind"] for v in volumes.values()}
    assert "/root/.cache/pip" in binds
    assert "/root/.cache/uv" in binds
    # Two distinct volumes (pip and uv layouts must not be mixed)
    assert len(volumes) == 2
    # Swap headroom: memswap_limit is above mem_limit, not equal to it
    assert captured["memswap_limit"] > captured["mem_limit"]


def test_module_defaults_allow_heavy_python_servers():
    import app.sandbox.container_manager as cm

    assert cm.CONTAINER_MEMORY_LIMIT >= 1024 * 1024 * 1024  # at least 1GiB
    assert cm.CONTAINER_MEMSWAP_LIMIT > cm.CONTAINER_MEMORY_LIMIT


# ---------------------------------------------------------------------------
# connect(): single automatic retry after an OOM kill
# ---------------------------------------------------------------------------

def test_connect_retries_once_after_oom_failure(monkeypatch):
    client = _client()
    attempts: list[str] = []

    async def fake_connect_once(_command, _env, _timeout):
        attempts.append("try")
        if len(attempts) == 1:
            return LocalConnectResult(
                connected=False, error="MCP server exited with code 137 (SIGKILL)",
                user_message=lm.LocalMCPClient._OOM_USER_MESSAGE,
            )
        return LocalConnectResult(connected=True, tools=[])

    monkeypatch.setattr(client, "_connect_once", fake_connect_once)
    monkeypatch.setattr(lm, "OOM_RETRY_DELAY_S", 0)

    result = asyncio.run(client.connect(["sh", "-c", "uvx x"], {}, container_id="abc"))

    assert result.connected is True
    assert len(attempts) == 2  # exactly one retry


def test_connect_does_not_retry_non_oom_failures(monkeypatch):
    client = _client()
    attempts: list[str] = []

    async def fake_connect_once(_command, _env, _timeout):
        attempts.append("try")
        return LocalConnectResult(
            connected=False, error="MCP server exited with code 1",
            user_message="This MCP server needs credentials (TOKEN). Add them and try again.",
            auth_reason="unauthorized",
        )

    monkeypatch.setattr(client, "_connect_once", fake_connect_once)

    result = asyncio.run(client.connect(["sh", "-c", "uvx x"], {}, container_id="abc"))

    assert result.connected is False
    assert result.auth_reason == "unauthorized"
    assert len(attempts) == 1  # no retry for auth/config failures


def test_connect_does_not_retry_twice(monkeypatch):
    client = _client()
    attempts: list[str] = []

    async def fake_connect_once(_command, _env, _timeout):
        attempts.append("try")
        return LocalConnectResult(
            connected=False, error="MCP server exited with code 137 (SIGKILL)",
            user_message=lm.LocalMCPClient._OOM_USER_MESSAGE,
        )

    monkeypatch.setattr(client, "_connect_once", fake_connect_once)
    monkeypatch.setattr(lm, "OOM_RETRY_DELAY_S", 0)

    result = asyncio.run(client.connect(["sh", "-c", "uvx x"], {}, container_id="abc"))

    assert result.connected is False
    assert len(attempts) == 2  # initial attempt + single retry, then give up
