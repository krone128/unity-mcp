"""
Port discovery utility for MCP for Unity Server.

What changed and why:
- Unity now writes a per-project port file named like
  `~/.unity-mcp/unity-mcp-port-<hash>.json` to avoid projects overwriting
  each other's saved port. The legacy file `unity-mcp-port.json` may still
  exist.
- This module now scans for both patterns, prefers the most recently
  modified file, and verifies that the port is actually a MCP for Unity listener
  (quick socket connect + ping) before choosing it.
"""

import glob
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import socket
import struct

from models.models import UnityInstanceInfo

logger = logging.getLogger("mcp-for-unity-server")


class PortDiscovery:
    """Handles port discovery from Unity Bridge registry"""
    REGISTRY_FILE = "unity-mcp-port.json"  # legacy single-project file
    DEFAULT_PORT = 6400
    CONNECT_TIMEOUT = 0.3  # seconds, keep this snappy during discovery

    # Stale status pruning: editors that were killed never delete their status file, so the
    # file keeps claiming a port that another editor may reuse. Set
    # UNITY_MCP_PRUNE_STALE_STATUS=0 to keep every file.
    RELOAD_GRACE_SECONDS = 60.0
    # A port has one listener and a live editor heartbeats every ~0.5 s: an entry that lost
    # its port to a fresher one and has not heartbeated for this long is dead.
    PORT_COLLISION_PRUNE_SECONDS = 60.0
    # An unreachable entry without a pid is only pruned after this long, so long imports,
    # modal dialogs and compiles that stall the heartbeat are never mistaken for a crash.
    UNREACHABLE_PRUNE_SECONDS = 30 * 60.0

    @staticmethod
    def get_registry_path() -> Path:
        """Get the path to the port registry file"""
        return PortDiscovery.get_registry_dir() / PortDiscovery.REGISTRY_FILE

    @staticmethod
    def get_registry_dir() -> Path:
        env_dir = os.environ.get("UNITY_MCP_STATUS_DIR")
        if env_dir:
            return Path(env_dir)
        return Path.home() / ".unity-mcp"

    @staticmethod
    def list_candidate_files() -> list[Path]:
        """Return candidate registry files, newest first.
        Includes hashed per-project files and the legacy file (if present).
        """
        base = PortDiscovery.get_registry_dir()
        hashed = sorted(
            (Path(p) for p in glob.glob(str(base / "unity-mcp-port-*.json"))),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        legacy = PortDiscovery.get_registry_path()
        if legacy.exists():
            # Put legacy at the end so hashed, per-project files win
            hashed.append(legacy)
        return hashed

    @staticmethod
    def _try_probe_unity_mcp(port: int) -> bool:
        """Quickly check if a MCP for Unity listener is on this port.
        Uses Unity's framed protocol: receives handshake, sends framed ping, expects framed pong.
        """
        try:
            with socket.create_connection(("127.0.0.1", port), PortDiscovery.CONNECT_TIMEOUT) as s:
                s.settimeout(PortDiscovery.CONNECT_TIMEOUT)
                try:
                    # 1. Receive handshake from Unity
                    handshake = s.recv(512)
                    if not handshake or b"FRAMING=1" not in handshake:
                        # Try legacy mode as fallback
                        s.sendall(b"ping")
                        data = s.recv(512)
                        return data and b'"message":"pong"' in data

                    # 2. Send framed ping command
                    # Frame format: 8-byte length header (big-endian uint64) + payload
                    payload = b"ping"
                    header = struct.pack('>Q', len(payload))
                    s.sendall(header + payload)

                    # 3. Receive framed response
                    # Helper to receive exact number of bytes
                    def _recv_exact(expected: int) -> bytes | None:
                        chunks = bytearray()
                        while len(chunks) < expected:
                            chunk = s.recv(expected - len(chunks))
                            if not chunk:
                                return None
                            chunks.extend(chunk)
                        return bytes(chunks)

                    response_header = _recv_exact(8)
                    if response_header is None:
                        return False

                    response_length = struct.unpack('>Q', response_header)[0]
                    if response_length > 10000:  # Sanity check
                        return False

                    response = _recv_exact(response_length)
                    if response is None:
                        return False
                    return b'"message":"pong"' in response
                except Exception as e:
                    logger.debug(f"Port probe failed for {port}: {e}")
                    return False
        except Exception as e:
            logger.debug(f"Connection failed for port {port}: {e}")
            return False

    @staticmethod
    def _read_latest_status() -> dict | None:
        try:
            base = PortDiscovery.get_registry_dir()
            status_files = sorted(
                (Path(p)
                 for p in glob.glob(str(base / "unity-mcp-status-*.json"))),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not status_files:
                return None
            with status_files[0].open('r') as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def discover_unity_port() -> int:
        """
        Discover Unity port by scanning per-project and legacy registry files.
        Prefer the newest file whose port responds; fall back to first parsed
        value; finally default to 6400.

        Returns:
            Port number to connect to
        """
        # Prefer the latest heartbeat status if it points to a responsive port
        status = PortDiscovery._read_latest_status()
        if status:
            port = status.get('unity_port')
            if isinstance(port, int) and PortDiscovery._try_probe_unity_mcp(port):
                logger.info(f"Using Unity port from status: {port}")
                return port

        candidates = PortDiscovery.list_candidate_files()

        first_seen_port: int | None = None

        for path in candidates:
            try:
                with open(path, 'r') as f:
                    cfg = json.load(f)
                unity_port = cfg.get('unity_port')
                if isinstance(unity_port, int):
                    if first_seen_port is None:
                        first_seen_port = unity_port
                    if PortDiscovery._try_probe_unity_mcp(unity_port):
                        logger.info(
                            f"Using Unity port from {path.name}: {unity_port}")
                        return unity_port
            except Exception as e:
                logger.warning(f"Could not read port registry {path}: {e}")

        if first_seen_port is not None:
            logger.info(
                f"No responsive port found; using first seen value {first_seen_port}")
            return first_seen_port

        # Fallback to default port
        logger.info(
            f"No port registry found; using default port {PortDiscovery.DEFAULT_PORT}")
        return PortDiscovery.DEFAULT_PORT

    @staticmethod
    def get_port_config() -> dict | None:
        """
        Get the most relevant port configuration from registry.
        Returns the most recent hashed file's config if present,
        otherwise the legacy file's config. Returns None if nothing exists.

        Returns:
            Port configuration dict or None if not found
        """
        candidates = PortDiscovery.list_candidate_files()
        if not candidates:
            return None
        for path in candidates:
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(
                    f"Could not read port configuration {path}: {e}")
        return None

    @staticmethod
    def _extract_project_name(project_path: str) -> str:
        """Extract project name from Assets path.

        Examples:
            /Users/sakura/Projects/MyGame/Assets -> MyGame
            C:\\Projects\\TestProject\\Assets -> TestProject
        """
        if not project_path:
            return "Unknown"

        try:
            # Remove trailing /Assets or \Assets
            path = project_path.rstrip('/\\')
            if path.endswith('Assets'):
                path = path[:-6].rstrip('/\\')

            # Get the last directory name
            name = os.path.basename(path)
            return name if name else "Unknown"
        except Exception:
            return "Unknown"

    @staticmethod
    def discover_all_unity_instances() -> list[UnityInstanceInfo]:
        """
        Discover all running Unity Editor instances by scanning status files.

        Returns:
            List of UnityInstanceInfo objects for all discovered instances
        """
        instances_by_port: dict[int, tuple[UnityInstanceInfo, datetime]] = {}
        base = PortDiscovery.get_registry_dir()
        prune = PortDiscovery._pruning_enabled()
        now = datetime.now(timezone.utc)

        # Scan all status files
        status_pattern = str(base / "unity-mcp-status-*.json")
        status_files = glob.glob(status_pattern)

        # Entries that answer on their port (or are reloading), grouped by port below
        candidates: list[tuple[Path, UnityInstanceInfo, datetime, bool]] = []

        for status_file_path in status_files:
            try:
                status_path = Path(status_file_path)
                file_mtime = datetime.fromtimestamp(
                    status_path.stat().st_mtime, tz=timezone.utc)

                with status_path.open('r') as f:
                    data = json.load(f)

                # Extract hash from filename: unity-mcp-status-{hash}.json
                filename = os.path.basename(status_file_path)
                hash_value = filename.replace(
                    'unity-mcp-status-', '').replace('.json', '')

                # Extract information
                project_path = data.get('project_path', '')
                project_name = PortDiscovery._extract_project_name(
                    project_path)
                port = data.get('unity_port')
                is_reloading = data.get('reloading', False)

                # Parse last_heartbeat
                last_heartbeat = None
                heartbeat_str = data.get('last_heartbeat')
                if heartbeat_str:
                    try:
                        last_heartbeat = datetime.fromisoformat(
                            heartbeat_str.replace('Z', '+00:00'))
                        if last_heartbeat.tzinfo is None:
                            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                freshness = last_heartbeat or file_mtime
                age_s = (now - freshness).total_seconds()

                # Killed editor: its process is gone (status files written with a pid on this host)
                if prune and PortDiscovery._is_same_host(data.get('host')) \
                        and PortDiscovery._is_process_alive(data.get('pid')) is False:
                    PortDiscovery._prune_status_file(
                        status_path, project_path, f"process {data.get('pid')} is gone")
                    continue

                # Deleted project (e.g. a removed worktree or temp project)
                if prune and PortDiscovery._is_project_gone(project_path):
                    PortDiscovery._prune_status_file(
                        status_path, project_path, "project no longer exists")
                    continue

                # Verify port is actually responding
                is_alive = PortDiscovery._try_probe_unity_mcp(
                    port) if isinstance(port, int) else False

                if not is_alive:
                    # If Unity says it's reloading and the status is fresh, don't drop the instance.
                    if is_reloading and age_s < PortDiscovery.RELOAD_GRACE_SECONDS:
                        pass  # keep it, status="reloading"
                    else:
                        logger.debug(
                            f"Instance {project_name}@{hash_value} has heartbeat but port {port} not responding")
                        if prune and age_s > PortDiscovery.UNREACHABLE_PRUNE_SECONDS:
                            PortDiscovery._prune_status_file(
                                status_path, project_path,
                                f"port {port} not responding for {age_s:.0f}s")
                        continue

                # Create instance info
                instance = UnityInstanceInfo(
                    id=f"{project_name}@{hash_value}",
                    name=project_name,
                    path=project_path,
                    hash=hash_value,
                    port=port,
                    status="reloading" if is_reloading else "running",
                    last_heartbeat=last_heartbeat,
                    # May not be available in current version
                    unity_version=data.get('unity_version'),
                    project_scoped_tools=data.get('project_scoped_tools', False),
                )
                candidates.append((status_path, instance, freshness, is_reloading))

            except Exception as e:
                logger.debug(
                    f"Failed to parse status file {status_file_path}: {e}")
                continue

        # One listener per port: the freshest heartbeat owns it, older claims are stale
        for status_path, instance, freshness, is_reloading in sorted(
                candidates, key=lambda item: item[2], reverse=True):
            existing = instances_by_port.get(instance.port)
            if existing:
                logger.debug(
                    f"Skipping stale status entry {status_path.name} in favor of more recent data for port {instance.port}")
                age_s = (now - freshness).total_seconds()
                if prune and not is_reloading and age_s > PortDiscovery.PORT_COLLISION_PRUNE_SECONDS:
                    PortDiscovery._prune_status_file(
                        status_path, instance.path,
                        f"port {instance.port} now belongs to {existing[0].id}")
                continue

            instances_by_port[instance.port] = (instance, freshness)
            logger.debug(
                f"Discovered Unity instance: {instance.id} on port {instance.port}")

        if prune:
            PortDiscovery._prune_orphan_port_files(base)

        deduped_instances = [entry[0] for entry in sorted(
            instances_by_port.values(), key=lambda item: item[1], reverse=True)]

        logger.info(
            f"Discovered {len(deduped_instances)} Unity instances (after de-duplication by port)")
        return deduped_instances

    # STALE FILE PRUNING

    @staticmethod
    def _pruning_enabled() -> bool:
        return os.environ.get("UNITY_MCP_PRUNE_STALE_STATUS", "1").strip().lower() not in ("0", "false", "no", "off")

    @staticmethod
    def _is_same_host(host) -> bool:
        """Whether the status file was written on this machine (pids are only meaningful there)."""
        if not isinstance(host, str) or not host.strip():
            return False
        local = socket.gethostname()
        return host.strip().split('.')[0].lower() == local.split('.')[0].lower()

    @staticmethod
    def _is_project_gone(project_path: str) -> bool:
        """The project folder was deleted while its parent folder still exists.

        Requiring the parent avoids pruning when the server simply cannot see the editor's
        filesystem (e.g. running in a container).
        """
        if not project_path:
            return False
        root = project_path.rstrip('/\\')
        if root.endswith('Assets'):
            root = root[:-6].rstrip('/\\')
        if not root:
            return False
        return not os.path.isdir(root) and os.path.isdir(os.path.dirname(root))

    @staticmethod
    def _is_process_alive(pid) -> bool | None:
        """True / False when the editor process can be checked, None when unknown.

        Status files from older Unity packages carry no pid. Windows is not probed:
        os.kill(pid, 0) terminates the process there instead of testing it.
        """
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if sys.platform == "win32":
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    @staticmethod
    def _prune_status_file(status_path: Path, project_path: str, reason: str) -> None:
        """Delete a dead editor's status file, and its port file when the project is gone."""
        try:
            status_path.unlink()
            logger.info(f"Pruned stale Unity status file {status_path.name}: {reason}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug(f"Could not prune {status_path.name}: {exc}")
            return

        # The port file is the project's saved port preference: keep it while the project exists
        if PortDiscovery._is_project_gone(project_path):
            hash_value = status_path.stem.replace('unity-mcp-status-', '')
            port_path = status_path.with_name(f"unity-mcp-port-{hash_value}.json")
            try:
                port_path.unlink()
                logger.info(f"Pruned port file {port_path.name}: project no longer exists")
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug(f"Could not prune {port_path.name}: {exc}")

    @staticmethod
    def _prune_orphan_port_files(base: Path) -> None:
        """Delete per-project port files whose project no longer exists."""
        for path_str in glob.glob(str(base / "unity-mcp-port-*.json")):
            port_path = Path(path_str)
            try:
                with port_path.open('r') as f:
                    project_path = json.load(f).get('project_path', '')
            except (OSError, ValueError):
                continue
            if PortDiscovery._is_project_gone(project_path):
                try:
                    port_path.unlink()
                    logger.info(f"Pruned port file {port_path.name}: project no longer exists")
                except OSError as exc:
                    logger.debug(f"Could not prune {port_path.name}: {exc}")
