"""Killed editors leave their status file behind; discovery prunes the ones that are provably dead."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from transport.legacy.port_discovery import PortDiscovery


@pytest.fixture
def registry(monkeypatch, tmp_path):
    base = tmp_path / ".unity-mcp"
    base.mkdir()
    monkeypatch.setattr(PortDiscovery, "get_registry_dir", staticmethod(lambda: base))
    monkeypatch.delenv("UNITY_MCP_PRUNE_STALE_STATUS", raising=False)
    return base


@pytest.fixture
def live_ports(monkeypatch):
    ports: set[int] = set()
    monkeypatch.setattr(PortDiscovery, "_try_probe_unity_mcp", staticmethod(lambda port: port in ports))
    return ports


def _project(tmp_path: Path, name: str) -> str:
    assets = tmp_path / "projects" / name / "Assets"
    assets.mkdir(parents=True, exist_ok=True)
    return str(assets)


def _write(base: Path, hash_value: str, project_path: str, port: int, age_s: float = 0.0,
           reloading: bool = False, **extra) -> tuple[Path, Path]:
    heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")
    status = base / f"unity-mcp-status-{hash_value}.json"
    status.write_text(json.dumps({
        "unity_port": port,
        "reloading": reloading,
        "project_path": project_path,
        "last_heartbeat": heartbeat,
        **extra,
    }))
    port_file = base / f"unity-mcp-port-{hash_value}.json"
    port_file.write_text(json.dumps({"unity_port": port, "project_path": project_path}))
    return status, port_file


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


HOST = socket.gethostname()


def test_live_editor_is_kept(registry, live_ports, tmp_path):
    status, port_file = _write(registry, "aaaa1111", _project(tmp_path, "Live"), 6401, pid=os.getpid(), host=HOST)
    live_ports.add(6401)

    ids = [i.id for i in PortDiscovery.discover_all_unity_instances()]

    assert ids == ["Live@aaaa1111"]
    assert status.exists() and port_file.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="pid liveness is not probed on Windows")
def test_dead_pid_is_pruned_even_when_its_port_answers(registry, live_ports, tmp_path):
    """The port answers because another editor reused it: the pid proves this file is dead."""
    status, port_file = _write(registry, "aaaa1111", _project(tmp_path, "Killed"), 6400, pid=_dead_pid(), host=HOST)
    live_ports.add(6400)

    assert PortDiscovery.discover_all_unity_instances() == []
    assert not status.exists()
    assert port_file.exists(), "the project still exists, keep its saved port"


def test_pid_from_another_host_is_ignored(registry, live_ports, tmp_path):
    status, _ = _write(registry, "aaaa1111", _project(tmp_path, "Remote"), 6401, pid=_dead_pid(), host="some-other-machine")
    live_ports.add(6401)

    assert [i.id for i in PortDiscovery.discover_all_unity_instances()] == ["Remote@aaaa1111"]
    assert status.exists()


def test_port_collision_prunes_the_older_claim(registry, live_ports, tmp_path):
    """Old package files (no pid): one port, two claims; the fresh one owns it."""
    old, _ = _write(registry, "aaaa1111", _project(tmp_path, "Old"), 6400, age_s=3600)
    new, _ = _write(registry, "bbbb2222", _project(tmp_path, "New"), 6400, age_s=0.2)
    live_ports.add(6400)

    assert [i.id for i in PortDiscovery.discover_all_unity_instances()] == ["New@bbbb2222"]
    assert not old.exists()
    assert new.exists()


def test_port_collision_keeps_a_recent_or_reloading_claim(registry, live_ports, tmp_path):
    recent, _ = _write(registry, "aaaa1111", _project(tmp_path, "Recent"), 6400, age_s=10)
    reloading, _ = _write(registry, "cccc3333", _project(tmp_path, "Reloading"), 6400, age_s=120, reloading=True)
    _write(registry, "bbbb2222", _project(tmp_path, "New"), 6400, age_s=0.2)
    live_ports.add(6400)

    PortDiscovery.discover_all_unity_instances()

    assert recent.exists()
    assert reloading.exists()


def test_unreachable_entry_is_pruned_only_when_very_old(registry, live_ports, tmp_path):
    stalled, _ = _write(registry, "aaaa1111", _project(tmp_path, "Stalled"), 6401, age_s=5 * 60)
    dead, _ = _write(registry, "bbbb2222", _project(tmp_path, "Dead"), 6402, age_s=PortDiscovery.UNREACHABLE_PRUNE_SECONDS + 60)

    assert PortDiscovery.discover_all_unity_instances() == []
    assert stalled.exists(), "a long import or modal dialog can stall the heartbeat for minutes"
    assert not dead.exists()


def test_deleted_project_prunes_status_and_port_file(registry, live_ports, tmp_path):
    gone = str(tmp_path / "projects" / "Removed" / "Assets")
    (tmp_path / "projects").mkdir(exist_ok=True)
    status, port_file = _write(registry, "aaaa1111", gone, 6401)
    orphan_port = registry / "unity-mcp-port-dddd4444.json"
    orphan_port.write_text(json.dumps({"unity_port": 6405, "project_path": str(tmp_path / "projects" / "Old" / "Assets")}))
    live_ports.add(6401)

    assert PortDiscovery.discover_all_unity_instances() == []
    assert not status.exists()
    assert not port_file.exists()
    assert not orphan_port.exists()


def test_unreadable_filesystem_is_not_mistaken_for_a_deleted_project(registry, live_ports):
    """E.g. a containerised server that cannot see the host paths: the parent is missing too."""
    status, port_file = _write(registry, "aaaa1111", "/no-such-root/Users/x/Game/Assets", 6401)
    live_ports.add(6401)

    assert [i.id for i in PortDiscovery.discover_all_unity_instances()] == ["Game@aaaa1111"]
    assert status.exists() and port_file.exists()


def test_pruning_can_be_disabled(registry, live_ports, tmp_path, monkeypatch):
    monkeypatch.setenv("UNITY_MCP_PRUNE_STALE_STATUS", "0")
    dead, _ = _write(registry, "bbbb2222", _project(tmp_path, "Dead"), 6402, age_s=PortDiscovery.UNREACHABLE_PRUNE_SECONDS + 60)

    PortDiscovery.discover_all_unity_instances()

    assert dead.exists()
