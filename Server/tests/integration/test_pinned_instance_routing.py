"""A pinned Unity instance must never be swapped for another editor while it reconnects.

With several editors open, an instance that is briefly missing from discovery (domain
reload, play mode, compile) used to fall back to the port scan, which returns whichever
editor heartbeated last: the retried command then ran in another project.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.config import config
import transport.legacy.unity_connection as uc
from models.models import UnityInstanceInfo
from transport.legacy.stdio_port_registry import StdioPortRegistry
from transport.legacy.unity_connection import UnityConnection

PINNED = "Pinned@aaaa1111"
OTHER = "Other@bbbb2222"


def _instance(instance_id: str, port: int) -> UnityInstanceInfo:
    name, _, hash_value = instance_id.partition("@")
    return UnityInstanceInfo(
        id=instance_id,
        name=name,
        path=f"/tmp/{name}/Assets",
        hash=hash_value,
        port=port,
        status="running",
    )


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(uc.time, "sleep", lambda _seconds: None)


def _registry(monkeypatch, discover) -> StdioPortRegistry:
    registry = StdioPortRegistry()
    monkeypatch.setattr(
        "transport.legacy.stdio_port_registry.PortDiscovery.discover_all_unity_instances",
        discover,
    )
    # The port scan answers with the other editor, as it does when that one heartbeated last
    monkeypatch.setattr(
        "transport.legacy.stdio_port_registry.PortDiscovery.discover_unity_port",
        lambda: 6402,
    )
    monkeypatch.setattr(uc, "stdio_port_registry", registry)
    return registry


def test_missing_pinned_instance_keeps_its_port(monkeypatch, no_sleep) -> None:
    """Only another editor is discoverable: retries stay on the pinned port and then fail."""
    _registry(monkeypatch, lambda: [_instance(OTHER, 6402)])

    conn = UnityConnection(port=6401, instance_id=PINNED)
    attempted_ports: list[int] = []

    def fail_connect(connect_timeout: float | None = None) -> bool:
        attempted_ports.append(conn.port)
        return False

    monkeypatch.setattr(conn, "connect", fail_connect)

    with pytest.raises(ConnectionError, match="Could not connect to Unity"):
        conn.send_command("get_editor_state", {}, max_attempts=3)

    assert attempted_ports == [6401, 6401, 6401, 6401]


def test_pinned_instance_back_on_new_port_is_followed(monkeypatch, no_sleep) -> None:
    """After the reload the instance reappears on a new port: retries follow it, not the other editor."""
    discovered: list[UnityInstanceInfo] = [_instance(OTHER, 6402)]
    _registry(monkeypatch, lambda: list(discovered))

    conn = UnityConnection(port=6401, instance_id=PINNED)
    attempted_ports: list[int] = []

    def fail_connect(connect_timeout: float | None = None) -> bool:
        attempted_ports.append(conn.port)
        if len(attempted_ports) == 2:
            discovered.append(_instance(PINNED, 6403))
        return False

    monkeypatch.setattr(conn, "connect", fail_connect)

    with pytest.raises(ConnectionError):
        conn.send_command("get_editor_state", {}, max_attempts=3)

    assert attempted_ports == [6401, 6401, 6403, 6403]
    assert 6402 not in attempted_ports


def test_unpinned_connection_still_uses_port_scan(monkeypatch, no_sleep) -> None:
    """Without an instance id the registry default is still used."""
    _registry(monkeypatch, lambda: [_instance(OTHER, 6402)])

    conn = UnityConnection(port=6401, instance_id=None)
    attempted_ports: list[int] = []

    def fail_connect(connect_timeout: float | None = None) -> bool:
        attempted_ports.append(conn.port)
        return False

    monkeypatch.setattr(conn, "connect", fail_connect)

    with pytest.raises(ConnectionError):
        conn.send_command("get_editor_state", {}, max_attempts=1)

    assert attempted_ports == [6401, 6402]


def _write_status(base: Path, hash_value: str, reloading: bool) -> None:
    status_dir = base / ".unity-mcp"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / f"unity-mcp-status-{hash_value}.json").write_text(
        json.dumps({"reloading": reloading, "reason": "reloading" if reloading else "", "project_path": "x"})
    )


def test_other_project_reloading_does_not_block_pinned_instance(monkeypatch, tmp_path, no_sleep) -> None:
    """The preflight reload check must read the pinned instance's own status, not the newest file."""
    monkeypatch.setattr(uc.Path, "home", lambda: Path(tmp_path))
    _write_status(Path(tmp_path), "bbbb2222", reloading=True)
    _registry(monkeypatch, lambda: [])
    monkeypatch.setattr(config, "command_total_timeout", 2.0, raising=False)

    conn = UnityConnection(port=6401, instance_id=PINNED)
    connects: list[int] = []

    def fail_connect(connect_timeout: float | None = None) -> bool:
        connects.append(conn.port)
        return False

    monkeypatch.setattr(conn, "connect", fail_connect)

    start = time.monotonic()
    with pytest.raises(ConnectionError):
        conn.send_command("get_editor_state", {}, max_attempts=0)

    assert connects == [6401], "the other project's reload status must not short-circuit the pinned command"
    assert time.monotonic() - start < 2.0


def test_pinned_instance_reloading_still_reports_reload(monkeypatch, tmp_path) -> None:
    """The pinned instance's own reload status keeps the structured retry hint."""
    monkeypatch.setattr(uc.Path, "home", lambda: Path(tmp_path))
    _write_status(Path(tmp_path), "aaaa1111", reloading=True)

    conn = UnityConnection(port=6401, instance_id=PINNED)
    response = conn.send_command("get_editor_state", {})

    assert uc._extract_response_reason(response) == "reloading"
