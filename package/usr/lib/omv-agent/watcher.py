#!/usr/bin/env python3
"""
==============================================================================
OMV Agent Watcher — Live System Observer Daemon
==============================================================================

ARCHITECTURE
------------
This daemon is the dedicated observer for the OMV Agent system. It runs as a
separate, maximally-hardened service (omv-agent-watch.service) and produces a
streaming event queue that the Flask backend (omv-agent.service) exposes to
the frontend widget via a polling endpoint.

Data flow:

    [kernel interfaces]  →  [Discovery Engine]  →  [Event Watchers]  →  [Brain]  →  [Event Queue]
     /proc, /sys,              enumerate all          tail journald,      classify,    /run/omv-agent/
     systemctl,                services/procs/        poll services,      deduplicate, event_queue.json
     journalctl               devices/nets            watch /sys/block    rate-limit

Each layer:
  1. Discovery Engine  — dynamically enumerates the live system. No hardcoded
                         lists. Called on slow_tick (30 s) to refresh state.
  2. Event Watchers    — JournaldWatcher tails kernel journal; ServiceState-
                         Watcher diffs systemd unit states; DeviceWatcher diffs
                         block-device set; ProbeWatcher reads the probe_cache.json
                         written by omv-agent-probe.service.
  3. Brain             — regex classifier for journal lines; Deduplicator prevents
                         flooding by rate-limiting repeated alerts per (type, source).
  4. Event Queue       — atomic JSON ring-buffer at /run/omv-agent/event_queue.json.
                         Max 100 events. Flask reads it directly; widget polls Flask.

SECURITY MODEL
--------------
  - NO subprocess calls that modify system state. All subprocess use is strictly
    read-only: systemctl list-units, journalctl -f, cat /proc/*.
  - NO os.system(). All external process calls use subprocess.run() or Popen().
  - NO network calls of any kind.
  - All subprocess calls receive an explicit, minimal PATH environment.
  - Every exception is caught and logged to stderr; the main loop NEVER crashes.
  - Runs as unprivileged user omv-agent-watch with PrivateNetwork=yes, no caps.
  - Writes only to /run/omv-agent/ (RuntimeDirectory created by systemd).
  - File permissions on event_queue.json: 0o644 (Flask www-data can read).

WHAT EACH WATCHER DOES
-----------------------
  JournaldWatcher   — Tails `journalctl -f -n 0 --output=json`. For each log
                      line the Brain classifier checks against error patterns
                      (disk I/O errors, OOM kills, filesystem corruption, RAID
                      events, auth failures). Non-matching lines are discarded.

  ServiceStateWatcher — Compares current systemd unit states (from Discovery
                        Engine) to the previous snapshot. Emits events for:
                        active→failed (service_failed), active→inactive
                        (service_stopped), inactive→active (service_started).

  DeviceWatcher     — Compares the current set of block devices (/sys/block/)
                      to the previous snapshot. Emits device_added / device_removed.
                      Filters out loop/ram/zram pseudo-devices.

  ProbeWatcher      — Reads /run/omv-agent/probe_cache.json (written by the
                      root probe daemon). Checks CPU load, drive temperatures,
                      disk usage %, RAID state, and bcache dirty ratio against
                      BRAIN_RULES thresholds. Emits graded warning/critical events.

EVENT QUEUE SCHEMA
------------------
Written to /run/omv-agent/event_queue.json:

{
  "events": [
    {
      "id":        "a3f1b2c4d5e6f789",   <- 16-char hex, os.urandom(8)
      "timestamp": 1234567890,            <- Unix epoch integer
      "level":     "warning",            <- "info" | "warning" | "critical"
      "type":      "service_failed",     <- event type string (see BRAIN_RULES)
      "source":    "smbd.service",       <- unit name, device, or "kernel"
      "msg":       "smbd.service entered failed state"
    }
  ],
  "last_updated": 1234567890
}

Max 100 events (ring buffer — oldest dropped when full).
Flask backend reads this file and serves it to the widget.
"""

# ==============================================================================
# SECTION 1 — BLUEPRINT & BRAIN CONSTANTS
# ==============================================================================
#
# The BRAIN_RULES dict is the single authoritative definition of all thresholds,
# timing parameters, and classification patterns used by this daemon. Changing
# alert sensitivity means changing a value here — nowhere else.
#
# Rationale for each threshold group:
#   load:   Pi 5 = 4 Cortex-A76 cores. Load 3.0 (warn) is 75% saturation —
#           noticeable slowdown. Load 3.8 (critical) is near-total saturation.
#   temp:   NVMe rated to ~70°C operating. Warning at 60°C gives headroom.
#           Critical at 70°C means the drive is at spec limit.
#   disk:   85% fill (warn) still leaves comfortable headroom for log rotation,
#           temp files, etc. 95% (critical) is emergency — writes will fail soon.
#   dedup:  Critical events (e.g. OOM kill) repeat at most every 60 s to avoid
#           alert storms. Informational events (service_started) suppress for
#           10 min since they have no urgency.
# ==============================================================================

import sys
import os
import re
import json
import time
import select
import signal
import logging
import tempfile
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Optional

# Logging goes to stderr — systemd/journald captures it under the watcher unit.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] omv-watcher: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("omv-watcher")

# Minimal environment for all subprocess calls — never inherit caller's PATH.
_SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}

BRAIN_RULES = {
    # ── Load thresholds (Pi 5: 4 cores) ───────────────────────────────────────
    "load": {
        "warn":     3.0,   # 75% of 4-core capacity
        "critical": 3.8,   # 95% — system is choking
    },

    # ── Drive temperature thresholds (°C) ─────────────────────────────────────
    "temp": {
        "warn":     60,    # warm but safe
        "critical": 70,    # at NVMe spec limit
    },

    # ── Disk usage thresholds (%) ─────────────────────────────────────────────
    "disk": {
        "warn":     85,    # start paying attention
        "critical": 95,    # near-full, writes will fail
    },

    # ── bcache dirty ratio threshold (%) ──────────────────────────────────────
    "bcache": {
        "dirty_warn": 70,  # writeback falling behind
    },

    # ── Deduplication TTL (seconds before same alert can repeat) ─────────────
    "dedup_ttl": {
        "critical": 60,    # 1 min — disk errors / OOM storms
        "warning":  300,   # 5 min — high load / warm drives
        "info":     600,   # 10 min — service started/stopped
    },

    # ── Ring buffer size ───────────────────────────────────────────────────────
    "max_events": 100,

    # ── Poll intervals (seconds) ───────────────────────────────────────────────
    "poll_interval_fast": 5,   # service states, probe cache, journal reads
    "poll_interval_slow": 30,  # full discovery (services, procs, devices)

    # ── Journal error patterns ─────────────────────────────────────────────────
    # These regexes are applied to journal MESSAGE fields. Patterns are compiled
    # once at startup (see _COMPILED_PATTERNS below). Each entry is a tuple of
    # (regex_string, event_type, level). Order matters: first match wins.
    "journal_error_patterns": [
        # Kernel disk I/O errors — highest priority, indicates hardware failure
        (r"I/O error",                  "disk_io_error",     "critical"),
        (r"ata\d+.*error",              "disk_io_error",     "critical"),
        (r"nvme.*error",                "disk_io_error",     "critical"),
        (r"SCSI.*error",                "disk_io_error",     "critical"),
        # Out-of-memory killer — system RAM exhausted
        (r"Out of memory",              "oom_kill",          "critical"),
        (r"oom_kill",                   "oom_kill",          "critical"),
        # Filesystem errors — indicate corruption or hardware failure
        (r"EXT4-fs error",              "fs_error",          "critical"),
        (r"XFS.*error",                 "fs_error",          "critical"),
        (r"BTRFS.*error",               "fs_error",          "critical"),
        # RAID events — degradation needs immediate attention
        (r"md/raid",                    "raid_event",        "warning"),
        (r"mdadm",                      "raid_event",        "warning"),
        # Auth failures — security monitoring
        (r"Failed password",            "auth_failure",      "warning"),
        (r"authentication failure",     "auth_failure",      "warning"),
    ],
}

# Pre-compile journal patterns for performance in the hot path.
_COMPILED_PATTERNS = [
    (re.compile(pat, re.IGNORECASE), evt_type, level)
    for pat, evt_type, level in BRAIN_RULES["journal_error_patterns"]
]


# ==============================================================================
# SECTION 2 — DISCOVERY ENGINE
# ==============================================================================
#
# These functions enumerate live system state dynamically. They are called on
# the slow_tick (every 30 seconds) to refresh the watcher's view of the system.
# No lists of service names are hardcoded — everything is discovered at runtime.
#
# All functions return empty structures (never raise) on any error, so the
# main loop can proceed even if a single discovery call fails (e.g. journalctl
# not available immediately at startup).
# ==============================================================================

def discover_services() -> dict:
    """
    Enumerate all systemd units of type 'service'.

    Runs: systemctl list-units --all --type=service --no-pager --output=json
    Returns: {unit_name: {"state": str, "sub_state": str, "description": str}}

    Falls back to text parsing if JSON output is unavailable (older systemd).
    Skips any units that fail JSON decoding gracefully.
    """
    result = {}
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--all", "--type=service",
             "--no-pager", "--output=json"],
            capture_output=True, text=True, timeout=10,
            env=_SAFE_ENV,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                units = json.loads(proc.stdout)
                for u in units:
                    name = u.get("unit", "")
                    if name:
                        result[name] = {
                            "state":       u.get("active", "unknown"),
                            "sub_state":   u.get("sub", "unknown"),
                            "description": u.get("description", ""),
                        }
                return result
            except (json.JSONDecodeError, TypeError):
                # JSON parse failed — fall through to text parsing
                pass

        # Text fallback: parse `systemctl list-units` tabular output.
        proc2 = subprocess.run(
            ["systemctl", "list-units", "--all", "--type=service",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=10,
            env=_SAFE_ENV,
        )
        for line in proc2.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].endswith(".service"):
                result[parts[0]] = {
                    "state":       parts[2],
                    "sub_state":   parts[3] if len(parts) > 3 else "unknown",
                    "description": " ".join(parts[4:]) if len(parts) > 4 else "",
                }
    except Exception as exc:
        log.warning("discover_services failed: %s", exc)
    return result


def discover_processes() -> dict:
    """
    Enumerate running processes from /proc.

    Reads /proc/{pid}/cmdline and /proc/{pid}/status for each numeric PID
    directory. Silently skips PIDs that disappear mid-scan (race condition
    between enumeration and read is normal) and PIDs we lack permission to read.

    Returns: {pid_int: {"name": str, "cmdline": str, "state": str}}
    """
    result = {}
    try:
        proc_dir = "/proc"
        for entry in os.listdir(proc_dir):
            if not entry.isdigit():
                continue
            pid = int(entry)
            pid_path = os.path.join(proc_dir, entry)
            name = ""
            cmdline = ""
            state = ""
            try:
                # Read cmdline — null-byte separated arguments
                cmdline_path = os.path.join(pid_path, "cmdline")
                with open(cmdline_path, "rb") as f:
                    raw = f.read(512)  # limit to first 512 bytes
                # Decode: null bytes → spaces, ignore decode errors
                cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
            try:
                # Read status — extract Name and State lines
                status_path = os.path.join(pid_path, "status")
                with open(status_path, "r", errors="replace") as f:
                    for line in f:
                        if line.startswith("Name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("State:"):
                            state = line.split(":", 1)[1].strip()
                        if name and state:
                            break
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
            if name or cmdline:
                result[pid] = {"name": name, "cmdline": cmdline, "state": state}
    except Exception as exc:
        log.warning("discover_processes failed: %s", exc)
    return result


def discover_block_devices() -> set:
    """
    Enumerate real block devices from /sys/block/.

    Excludes loop devices (loop*), RAM disks (ram*), and zram (zram*) since
    these are not physical storage devices and would generate spurious events.

    Returns: set of device name strings, e.g. {"nvme0n1", "sda", "bcache0"}
    """
    devices = set()
    try:
        for name in os.listdir("/sys/block"):
            # Skip pseudo-devices
            if (name.startswith("loop") or
                    name.startswith("ram") or
                    name.startswith("zram")):
                continue
            devices.add(name)
    except Exception as exc:
        log.warning("discover_block_devices failed: %s", exc)
    return devices


def discover_network_interfaces() -> dict:
    """
    Enumerate network interfaces and their operational state.

    Reads /sys/class/net/{iface}/operstate for each interface. The operstate
    file contains "up", "down", "unknown", "dormant", etc.

    Returns: {iface_name: state_string}
    """
    interfaces = {}
    try:
        net_dir = "/sys/class/net"
        for iface in os.listdir(net_dir):
            operstate_path = os.path.join(net_dir, iface, "operstate")
            try:
                with open(operstate_path, "r") as f:
                    state = f.read().strip()
            except (PermissionError, FileNotFoundError):
                state = "unknown"
            interfaces[iface] = state
    except Exception as exc:
        log.warning("discover_network_interfaces failed: %s", exc)
    return interfaces


def discover_config_files(processes: dict) -> dict:
    """
    Extract configuration file paths referenced in process command lines.

    For each process, scans cmdline tokens for:
      --config=<path>, -c <path>, and any token ending in .conf/.yaml/.json
      that looks like an absolute path (starts with /).

    Returns: {pid_int: [config_path_strings]}
    Useful for future features that watch config files for changes.
    """
    config_map = {}
    # Patterns that introduce a config file path
    _flag_re = re.compile(r"(?:--config=|--conf=|-c\s+)(/[^\s]+)")
    _ext_re  = re.compile(r"(/[^\s]+\.(?:conf|yaml|yml|json|toml|ini|cfg))")
    for pid, info in processes.items():
        cmdline = info.get("cmdline", "")
        if not cmdline:
            continue
        found = []
        found.extend(_flag_re.findall(cmdline))
        found.extend(_ext_re.findall(cmdline))
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for p in found:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        if unique:
            config_map[pid] = unique
    return config_map


# ==============================================================================
# SECTION 3 — EVENT WATCHERS
# ==============================================================================
#
# Watchers observe a specific aspect of system state. They return lists of
# Event objects which the orchestrator then passes through the Brain for
# classification and deduplication before queuing.
#
# Each watcher is self-contained and safe to call repeatedly. Errors inside
# a watcher do not propagate — they are caught, logged, and return an empty list.
# ==============================================================================

@dataclass
class Event:
    """
    Canonical event structure. Every alert in the system is an Event.

    Fields:
      id        — 16 hex chars from os.urandom(8), globally unique
      timestamp — Unix epoch integer (int(time.time()))
      level     — "info" | "warning" | "critical"
      type      — event type slug (e.g. "service_failed", "disk_io_error")
      source    — origin identifier (unit name, device path, or "kernel")
      msg       — human-readable description, kept short (<= 200 chars)
    """
    id:        str
    timestamp: int
    level:     str   # "info" | "warning" | "critical"
    type:      str
    source:    str
    msg:       str


def _make_event(level: str, type_: str, source: str, msg: str) -> Event:
    """Factory for Event objects with auto-generated id and timestamp."""
    return Event(
        id=os.urandom(8).hex(),
        timestamp=int(time.time()),
        level=level,
        type=type_,
        source=source,
        msg=msg[:200],
    )


class JournaldWatcher:
    """
    Tails the systemd journal in real time using `journalctl -f -n 0 --output=json`.

    The -n 0 flag means "start from now" — we don't replay old events on
    startup, which would flood the queue with stale alerts.

    Uses select() for non-blocking reads so the main loop never hangs waiting
    for journal output. Each call to read_events() drains whatever lines are
    currently available in the pipe buffer.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._start()

    def _start(self):
        """Launch journalctl subprocess. Safe to call after a restart."""
        try:
            self._proc = subprocess.Popen(
                ["journalctl", "-f", "-n", "0", "--output=json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_SAFE_ENV,
            )
            log.info("JournaldWatcher: tailing journal (pid=%d)", self._proc.pid)
        except Exception as exc:
            log.error("JournaldWatcher: failed to start journalctl: %s", exc)
            self._proc = None

    def read_events(self, timeout: float = 0.1) -> list:
        """
        Non-blocking drain of available journal lines.

        Uses select() to check if stdout has data before reading. Returns
        a list of raw journal entry dicts (parsed from JSON). Lines that
        fail JSON parsing are silently discarded — malformed lines do occur
        in the journal stream.

        Args:
            timeout: seconds to wait for data via select() (default 0.1 s)
        Returns:
            list of journal entry dicts
        """
        entries = []
        if self._proc is None or self._proc.poll() is not None:
            # Process died — attempt restart on next call
            log.warning("JournaldWatcher: journalctl process ended, restarting")
            self._start()
            return entries
        try:
            readable, _, _ = select.select([self._proc.stdout], [], [], timeout)
            if not readable:
                return entries
            # Read all available lines without blocking
            while True:
                ready, _, _ = select.select([self._proc.stdout], [], [], 0)
                if not ready:
                    break
                line = self._proc.stdout.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line.decode("utf-8", errors="replace"))
                    entries.append(entry)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        except Exception as exc:
            log.warning("JournaldWatcher.read_events error: %s", exc)
        return entries

    def close(self):
        """Terminate the journalctl subprocess gracefully."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            log.info("JournaldWatcher: journalctl process terminated")
        self._proc = None


class ServiceStateWatcher:
    """
    Detects systemd service state transitions by diffing snapshots.

    Tracks the previous state of every discovered service. On each update()
    call, compares the new state map to the old one and emits events for
    meaningful transitions.

    Transitions monitored:
      active → failed    → service_failed   (critical)
      active → inactive  → service_stopped  (info)
      inactive → active  → service_started  (info)
      failed  → active   → service_recovered (info)

    First-call (empty previous state) is treated as baseline — no events
    are emitted for the initial snapshot to avoid flooding on startup.
    """

    def __init__(self):
        self._previous: dict = {}
        self._initialized: bool = False

    def update(self, services: dict) -> list:
        """
        Compare new service state map to previous snapshot.

        Args:
            services: dict from discover_services()
        Returns:
            list of Event objects for state transitions
        """
        events = []
        if not self._initialized:
            # First call: establish baseline, emit nothing.
            self._previous = {k: v.copy() for k, v in services.items()}
            self._initialized = True
            return events

        try:
            # Check for transitions in known services
            for unit, info in services.items():
                new_state = info.get("state", "unknown")
                old_info  = self._previous.get(unit)
                old_state = old_info.get("state", "unknown") if old_info else None

                if old_state is None:
                    # New unit appeared — only emit if it's already failed
                    if new_state == "failed":
                        events.append(_make_event(
                            "critical", "service_failed", unit,
                            f"{unit} entered failed state",
                        ))
                    continue

                if new_state == old_state:
                    continue  # No transition

                if new_state == "failed":
                    events.append(_make_event(
                        "critical", "service_failed", unit,
                        f"{unit} entered failed state",
                    ))
                elif old_state == "active" and new_state == "inactive":
                    events.append(_make_event(
                        "info", "service_stopped", unit,
                        f"{unit} stopped (active → inactive)",
                    ))
                elif old_state in ("inactive", "failed") and new_state == "active":
                    level = "info" if old_state == "inactive" else "warning"
                    etype = "service_started" if old_state == "inactive" else "service_recovered"
                    events.append(_make_event(
                        level, etype, unit,
                        f"{unit} is now {new_state} (was {old_state})",
                    ))

            # Check for services that disappeared entirely
            for unit in list(self._previous.keys()):
                if unit not in services:
                    old_state = self._previous[unit].get("state", "unknown")
                    if old_state == "active":
                        events.append(_make_event(
                            "warning", "service_disappeared", unit,
                            f"{unit} disappeared from systemd unit list",
                        ))

        except Exception as exc:
            log.warning("ServiceStateWatcher.update error: %s", exc)

        # Always update snapshot to current state
        self._previous = {k: v.copy() for k, v in services.items()}
        return events


class DeviceWatcher:
    """
    Detects block device additions and removals by diffing /sys/block/ snapshots.

    Uses a simple set comparison: new_devices - old_devices = added,
    old_devices - new_devices = removed.

    First call establishes the baseline (no events emitted) to avoid reporting
    all existing drives as "added" on daemon startup.
    """

    def __init__(self):
        self._previous: set = set()
        self._initialized: bool = False

    def update(self, devices: set) -> list:
        """
        Compare new device set to previous snapshot.

        Args:
            devices: set from discover_block_devices()
        Returns:
            list of Event objects for device additions/removals
        """
        events = []
        if not self._initialized:
            self._previous = set(devices)
            self._initialized = True
            return events
        try:
            added   = devices - self._previous
            removed = self._previous - devices

            for dev in sorted(added):
                events.append(_make_event(
                    "info", "device_added", dev,
                    f"Block device {dev} appeared in /sys/block/",
                ))
                log.info("Device added: %s", dev)

            for dev in sorted(removed):
                events.append(_make_event(
                    "warning", "device_removed", dev,
                    f"Block device {dev} disappeared from /sys/block/",
                ))
                log.warning("Device removed: %s", dev)

        except Exception as exc:
            log.warning("DeviceWatcher.update error: %s", exc)

        self._previous = set(devices)
        return events


class ProbeWatcher:
    """
    Threshold monitor that reads the probe_cache.json written by omv-agent-probe.

    The probe daemon (running as root) writes comprehensive system stats to
    /run/omv-agent/probe_cache.json every 5-30 seconds. This watcher reads
    that file and compares values against BRAIN_RULES thresholds.

    Checks performed on each call to check():
      - CPU load (1-minute average) vs load warn/critical thresholds
      - Drive temperatures vs temp warn/critical thresholds
      - Disk usage percentages vs disk warn/critical thresholds
      - RAID array state (degraded / failed devices)
      - bcache dirty data ratio (if bcache devices present)

    The watcher does NOT directly probe hardware — it only reads the cache.
    This maintains the strict privilege separation: root probe writes, watcher reads.
    """

    PROBE_CACHE_PATH = "/run/omv-agent/probe_cache.json"
    MAX_CACHE_AGE    = 120  # seconds — if older than 2 min, treat as stale

    def check(self) -> list:
        """
        Read probe cache and emit events for any threshold violations.

        Returns:
            list of Event objects (empty if cache is stale or all values nominal)
        """
        events = []
        cache  = self._load_cache()
        if cache is None:
            return events

        try:
            events.extend(self._check_load(cache))
            events.extend(self._check_drive_temps(cache))
            events.extend(self._check_disk_usage(cache))
            events.extend(self._check_raid(cache))
            events.extend(self._check_bcache(cache))
        except Exception as exc:
            log.warning("ProbeWatcher.check error: %s", exc)
        return events

    def _load_cache(self) -> Optional[dict]:
        """Load probe cache JSON. Returns None if missing, stale, or corrupt."""
        try:
            if not os.path.exists(self.PROBE_CACHE_PATH):
                return None
            age = time.time() - os.path.getmtime(self.PROBE_CACHE_PATH)
            if age > self.MAX_CACHE_AGE:
                return None
            with open(self.PROBE_CACHE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _check_load(self, cache: dict) -> list:
        """Check CPU load average against BRAIN_RULES thresholds."""
        events = []
        try:
            system = cache.get("system", {})
            load_str = str(system.get("load_1m", "0"))
            load = float(load_str)
            thresholds = BRAIN_RULES["load"]
            if load >= thresholds["critical"]:
                events.append(_make_event(
                    "critical", "high_load", "system",
                    f"CPU load critical: {load:.2f} (threshold {thresholds['critical']})",
                ))
            elif load >= thresholds["warn"]:
                events.append(_make_event(
                    "warning", "high_load", "system",
                    f"CPU load elevated: {load:.2f} (threshold {thresholds['warn']})",
                ))
        except (ValueError, TypeError):
            pass
        return events

    def _check_drive_temps(self, cache: dict) -> list:
        """Check drive temperatures against BRAIN_RULES temp thresholds."""
        events = []
        try:
            drives = cache.get("drives", {})
            thresholds = BRAIN_RULES["temp"]
            for device, info in drives.items():
                temp = info.get("temperature_c")
                if not isinstance(temp, (int, float)):
                    continue
                if temp >= thresholds["critical"]:
                    events.append(_make_event(
                        "critical", "drive_temp_critical", device,
                        f"{device} temperature critical: {temp}°C "
                        f"(threshold {thresholds['critical']}°C)",
                    ))
                elif temp >= thresholds["warn"]:
                    events.append(_make_event(
                        "warning", "drive_temp_warn", device,
                        f"{device} temperature elevated: {temp}°C "
                        f"(threshold {thresholds['warn']}°C)",
                    ))
        except Exception as exc:
            log.debug("_check_drive_temps error: %s", exc)
        return events

    def _check_disk_usage(self, cache: dict) -> list:
        """
        Parse df output from probe cache and check usage percentages.

        The probe cache stores disk_usage as a formatted df -h string.
        We parse the Use% column from each data row.
        """
        events = []
        try:
            disks = cache.get("disks", {})
            df_output = disks.get("disk_usage", "")
            thresholds = BRAIN_RULES["disk"]
            # Parse df output: columns are Filesystem, Size, Used, Avail, Use%, Mounted
            for line in df_output.splitlines():
                parts = line.split()
                if len(parts) < 6:
                    continue
                # Use% is the second-to-last field when Mounted on is simple
                pct_str = parts[-2] if parts[-2].endswith("%") else None
                if pct_str is None:
                    # Try all parts for a % field
                    for p in parts:
                        if p.endswith("%") and p[:-1].isdigit():
                            pct_str = p
                            break
                if pct_str is None:
                    continue
                try:
                    pct = int(pct_str.rstrip("%"))
                except ValueError:
                    continue
                mountpoint = parts[-1]
                filesystem = parts[0]
                source = f"{filesystem} ({mountpoint})"
                if pct >= thresholds["critical"]:
                    events.append(_make_event(
                        "critical", "disk_full", source,
                        f"Disk critical: {source} at {pct}% full",
                    ))
                elif pct >= thresholds["warn"]:
                    events.append(_make_event(
                        "warning", "disk_full", source,
                        f"Disk warning: {source} at {pct}% full",
                    ))
        except Exception as exc:
            log.debug("_check_disk_usage error: %s", exc)
        return events

    def _check_raid(self, cache: dict) -> list:
        """Check RAID array states for degradation or failed devices."""
        events = []
        try:
            raid = cache.get("raid", {})
            if raid.get("raid") == "none":
                return events
            arrays = raid.get("raid_arrays", {})
            for array_name, info in arrays.items():
                detail = info.get("detail", "")
                # Check array state
                state_m = re.search(r"State\s*:\s*(.+)", detail)
                if state_m:
                    state = state_m.group(1).strip().lower()
                    if "degraded" in state:
                        events.append(_make_event(
                            "critical", "raid_degraded", array_name,
                            f"RAID array {array_name} is DEGRADED: {state}",
                        ))
                    elif "failed" in state:
                        events.append(_make_event(
                            "critical", "raid_degraded", array_name,
                            f"RAID array {array_name} has FAILED: {state}",
                        ))
                # Check failed device count
                failed_m = re.search(r"Failed Devices\s*:\s*(\d+)", detail)
                if failed_m and int(failed_m.group(1)) > 0:
                    n_failed = int(failed_m.group(1))
                    events.append(_make_event(
                        "critical", "raid_degraded", array_name,
                        f"RAID {array_name}: {n_failed} failed device(s)",
                    ))
        except Exception as exc:
            log.debug("_check_raid error: %s", exc)
        return events

    def _check_bcache(self, cache: dict) -> list:
        """Check bcache dirty data ratio against the configured threshold."""
        events = []
        try:
            bcache = cache.get("bcache", {})
            if bcache.get("_status") == "no bcache devices found":
                return events
            dirty_warn_pct = BRAIN_RULES["bcache"]["dirty_warn"]
            for dev, info in bcache.items():
                if dev == "_status":
                    continue
                pct = info.get("writeback_percent")
                if pct is None:
                    continue
                try:
                    pct_val = float(str(pct).strip("%"))
                except (ValueError, TypeError):
                    continue
                if pct_val >= dirty_warn_pct:
                    events.append(_make_event(
                        "warning", "bcache_dirty_high", dev,
                        f"bcache {dev} dirty data at {pct_val:.1f}% "
                        f"(threshold {dirty_warn_pct}%)",
                    ))
        except Exception as exc:
            log.debug("_check_bcache error: %s", exc)
        return events


# ==============================================================================
# SECTION 4 — BRAIN (Classifier + Deduplicator)
# ==============================================================================
#
# The Brain has two responsibilities:
#   1. classify_journal_event() — convert raw journal entry dicts into Events.
#      Uses the pre-compiled BRAIN_RULES patterns. Returns None for entries
#      that don't match any pattern (the vast majority of journal lines).
#   2. Deduplicator — prevents alert storms. Uses per-(type, source) TTL windows
#      from BRAIN_RULES["dedup_ttl"]. Critical events can repeat every 60 s;
#      informational events are suppressed for 10 minutes.
#
# The deduplicator runs AFTER classification, so each watcher's output is
# independently filtered before reaching the event queue.
# ==============================================================================

def classify_journal_event(entry: dict) -> Optional[Event]:
    """
    Classify a raw journal entry dict into an Event, or return None.

    Checks entry["MESSAGE"] against the pre-compiled BRAIN_RULES patterns.
    First match wins (patterns are ordered by priority in BRAIN_RULES).

    Source identification priority:
      1. _SYSTEMD_UNIT   — for service-originated messages
      2. SYSLOG_IDENTIFIER — for syslog-tagged messages
      3. "kernel"         — fallback for kernel messages

    Args:
        entry: raw dict from JournaldWatcher.read_events()
    Returns:
        Event on match, None otherwise
    """
    message = entry.get("MESSAGE", "")
    if not message or not isinstance(message, str):
        return None

    for pattern, evt_type, level in _COMPILED_PATTERNS:
        if pattern.search(message):
            source = (
                entry.get("_SYSTEMD_UNIT") or
                entry.get("SYSLOG_IDENTIFIER") or
                "kernel"
            )
            # Truncate message to keep events terse
            short_msg = message[:180].strip()
            return _make_event(level, evt_type, source, short_msg)

    return None


class Deduplicator:
    """
    Rate-limits repeated events by (type, source) pair using TTL windows.

    TTL values come from BRAIN_RULES["dedup_ttl"]:
      critical: 60 s   — high-priority alerts can repeat every minute
      warning:  300 s  — moderate alerts every 5 minutes
      info:     600 s  — informational events every 10 minutes

    The dedup key is (event.type, event.source) — two events for the same
    type from different sources (e.g. two different drives both overheating)
    are treated as distinct and each get their own TTL.
    """

    def __init__(self):
        # Maps (type, source) → last emit timestamp
        self._last_emit: dict = {}

    def should_emit(self, event: Event) -> bool:
        """
        Return True if this event should be emitted (not suppressed by TTL).

        Args:
            event: Event to check
        Returns:
            True if the event should be added to the queue
        """
        key = (event.type, event.source)
        last = self._last_emit.get(key)
        if last is None:
            return True  # Never emitted — always allow
        ttl = BRAIN_RULES["dedup_ttl"].get(event.level, 300)
        return (time.time() - last) >= ttl

    def record(self, event: Event):
        """
        Record that an event was emitted. Call this only for events that
        actually made it to the queue (i.e. after should_emit returned True).

        Args:
            event: Event that was just queued
        """
        key = (event.type, event.source)
        self._last_emit[key] = time.time()

    def prune(self):
        """
        Remove stale dedup entries to prevent unbounded memory growth.

        Called periodically by the orchestrator. Removes entries where the
        TTL has expired and the entry is no longer doing any suppression work.
        """
        now = time.time()
        max_ttl = max(BRAIN_RULES["dedup_ttl"].values())  # 600 s
        stale_keys = [k for k, ts in self._last_emit.items()
                      if (now - ts) > max_ttl * 2]
        for k in stale_keys:
            del self._last_emit[k]


# ==============================================================================
# SECTION 5 — EVENT QUEUE
# ==============================================================================
#
# The EventQueue is the shared data structure between the watcher daemon and
# the Flask backend. It is a JSON file at a well-known path in the systemd
# RuntimeDirectory (/run/omv-agent/).
#
# Design decisions:
#   - Atomic writes: write to a tmp file in the same directory, then os.replace().
#     This prevents Flask from reading a partial/corrupt file.
#   - Ring buffer: oldest events are dropped when max_events is reached.
#     The queue is not a history log — it represents current system health.
#   - Permissions: 0o644 so Flask (running as www-data) can read without
#     requiring group membership in omv-agent-watch.
#   - The queue directory is created by systemd's RuntimeDirectory= directive,
#     so it is guaranteed to exist before this code runs.
# ==============================================================================

class EventQueue:
    """
    Atomic JSON ring-buffer at /run/omv-agent/event_queue.json.

    Thread-safe for single-writer use (this daemon is single-threaded).
    Readable by any process with filesystem access to the file (0o644).
    """

    QUEUE_PATH = "/run/omv-agent/event_queue.json"
    QUEUE_DIR  = "/run/omv-agent"

    def _load(self) -> dict:
        """
        Load the current queue from disk.

        Returns the canonical empty structure on any error (missing file,
        corrupt JSON, permission denied) so push() always has a valid base.
        """
        empty = {"events": [], "last_updated": 0}
        try:
            with open(self.QUEUE_PATH, "r") as f:
                data = json.load(f)
            # Validate top-level structure
            if not isinstance(data.get("events"), list):
                return empty
            return data
        except (FileNotFoundError, json.JSONDecodeError, Exception):
            return empty

    def _write(self, data: dict):
        """
        Write queue data atomically using a temp file + os.replace().

        Creates the temp file in the same directory as the target so that
        os.replace() is guaranteed to be atomic (same filesystem).
        Sets permissions to 0o644 before the rename so the final file
        is readable by www-data even if umask is restrictive.
        """
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.QUEUE_DIR, prefix=".eq_")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, separators=(",", ":"))
                os.chmod(tmp_path, 0o644)
                os.replace(tmp_path, self.QUEUE_PATH)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
        except Exception as exc:
            log.error("EventQueue._write failed: %s", exc)

    def push(self, event: Event):
        """
        Append a single event to the queue, enforcing the ring-buffer limit.

        Args:
            event: Event to append
        """
        data = self._load()
        data["events"].append(asdict(event))
        max_ev = BRAIN_RULES["max_events"]
        if len(data["events"]) > max_ev:
            # Drop oldest — ring buffer behaviour
            data["events"] = data["events"][-max_ev:]
        data["last_updated"] = int(time.time())
        self._write(data)

    def push_many(self, events: list):
        """
        Batch append multiple events in a single read-modify-write cycle.

        More efficient than calling push() in a loop because it only reads
        and writes the file once regardless of how many events are pushed.

        Args:
            events: list of Event objects
        """
        if not events:
            return
        data = self._load()
        for event in events:
            data["events"].append(asdict(event))
        max_ev = BRAIN_RULES["max_events"]
        if len(data["events"]) > max_ev:
            data["events"] = data["events"][-max_ev:]
        data["last_updated"] = int(time.time())
        self._write(data)
        log.debug("EventQueue: pushed %d event(s)", len(events))


# ==============================================================================
# SECTION 6 — MAIN ORCHESTRATOR
# ==============================================================================
#
# WatcherDaemon is the top-level controller. It owns all watcher instances
# and drives two tick rates:
#
#   fast_tick (every 5 s):
#     - Read new journal lines from JournaldWatcher
#     - Classify journal entries via Brain
#     - Check ServiceStateWatcher for state transitions
#     - Check ProbeWatcher for threshold violations
#     - Apply deduplication to all candidate events
#     - Batch-push surviving events to EventQueue
#
#   slow_tick (every 30 s):
#     - Run full Discovery Engine (discover_services, discover_processes,
#       discover_block_devices) to refresh the watcher's system view
#     - Feed fresh service/device maps into ServiceStateWatcher, DeviceWatcher
#     - Prune deduplicator's stale entries
#
# The main loop sleeps in 1-second increments to remain responsive to SIGTERM.
# A shutdown flag is set by the signal handler so the loop exits cleanly
# without waiting for a full 5-second sleep to expire.
# ==============================================================================

class WatcherDaemon:
    """
    Main orchestrator for the OMV Agent watcher daemon.

    Manages the lifecycle of all watchers and drives the dual-speed
    polling loop. Designed to run for the lifetime of the system
    without memory growth or CPU hogging.
    """

    def __init__(self):
        log.info("WatcherDaemon initializing")

        # Core components
        self._queue       = EventQueue()
        self._dedup       = Deduplicator()
        self._journal     = JournaldWatcher()
        self._svc_watcher = ServiceStateWatcher()
        self._dev_watcher = DeviceWatcher()
        self._probe       = ProbeWatcher()

        # Timing state
        self._fast_interval = BRAIN_RULES["poll_interval_fast"]   # 5 s
        self._slow_interval = BRAIN_RULES["poll_interval_slow"]   # 30 s
        self._last_fast     = 0.0
        self._last_slow     = 0.0

        # Shutdown coordination
        self._running = True

        # Current discovery state (refreshed on slow_tick)
        self._services: dict = {}
        self._devices:  set  = set()
        self._procs:    dict = {}

        log.info("WatcherDaemon initialized (fast=%ds, slow=%ds)",
                 self._fast_interval, self._slow_interval)

    # ── Shutdown handling ───────────────────────────────────────────────────

    def stop(self):
        """Signal the daemon to stop gracefully after the current iteration."""
        log.info("WatcherDaemon: stop requested")
        self._running = False

    # ── Internal helpers ────────────────────────────────────────────────────

    def _filter_and_queue(self, events: list):
        """
        Apply deduplication to a list of events and push survivors to the queue.

        Args:
            events: list of Event objects from any watcher
        """
        survivors = []
        for event in events:
            if self._dedup.should_emit(event):
                self._dedup.record(event)
                survivors.append(event)
                log.info("EVENT [%s/%s] %s: %s",
                          event.level, event.type, event.source, event.msg)
        if survivors:
            self._queue.push_many(survivors)

    def _run_fast_tick(self):
        """
        Fast tick: journal reads, service state checks, probe threshold checks.
        Runs every poll_interval_fast seconds (default 5 s).
        """
        all_events = []

        # 1. Journal: read available lines and classify
        try:
            journal_entries = self._journal.read_events(timeout=0.05)
            for entry in journal_entries:
                event = classify_journal_event(entry)
                if event is not None:
                    all_events.append(event)
        except Exception as exc:
            log.warning("fast_tick journal error: %s", exc)

        # 2. Service state transitions (uses last-known discovery snapshot)
        try:
            if self._services:
                svc_events = self._svc_watcher.update(self._services)
                all_events.extend(svc_events)
        except Exception as exc:
            log.warning("fast_tick service state error: %s", exc)

        # 3. Probe cache threshold checks
        try:
            probe_events = self._probe.check()
            all_events.extend(probe_events)
        except Exception as exc:
            log.warning("fast_tick probe check error: %s", exc)

        # Filter through deduplicator and push to queue
        self._filter_and_queue(all_events)

    def _run_slow_tick(self):
        """
        Slow tick: full system discovery, device diffing, dedup pruning.
        Runs every poll_interval_slow seconds (default 30 s).
        """
        log.debug("WatcherDaemon: running slow tick (discovery)")

        # 1. Discover services (used by fast tick ServiceStateWatcher)
        try:
            self._services = discover_services()
            log.debug("Discovered %d services", len(self._services))
        except Exception as exc:
            log.warning("slow_tick discover_services error: %s", exc)

        # 2. Discover block devices and check for additions/removals
        try:
            new_devices = discover_block_devices()
            dev_events  = self._dev_watcher.update(new_devices)
            self._devices = new_devices
            self._filter_and_queue(dev_events)
            log.debug("Discovered %d block devices", len(self._devices))
        except Exception as exc:
            log.warning("slow_tick discover_block_devices error: %s", exc)

        # 3. Discover processes (used for config file tracking / future features)
        try:
            self._procs = discover_processes()
            log.debug("Discovered %d processes", len(self._procs))
        except Exception as exc:
            log.warning("slow_tick discover_processes error: %s", exc)

        # 4. Prune stale deduplicator entries to prevent memory growth
        try:
            self._dedup.prune()
        except Exception as exc:
            log.warning("slow_tick dedup prune error: %s", exc)

    # ── Main loop ───────────────────────────────────────────────────────────

    def run(self):
        """
        Main event loop. Runs until stop() is called or a signal is received.

        Sleeps in 1-second increments to remain responsive to SIGTERM.
        Both tick functions catch all exceptions internally, so the loop
        will never exit due to a watcher error.
        """
        log.info("WatcherDaemon: entering main loop")

        # Run an initial slow tick immediately to populate discovery state
        # before the first fast tick queries it.
        self._run_slow_tick()

        while self._running:
            now = time.monotonic()

            # Fast tick
            if (now - self._last_fast) >= self._fast_interval:
                self._last_fast = now
                try:
                    self._run_fast_tick()
                except Exception as exc:
                    log.error("fast_tick unhandled exception: %s", exc, exc_info=True)

            # Slow tick
            if (now - self._last_slow) >= self._slow_interval:
                self._last_slow = now
                try:
                    self._run_slow_tick()
                except Exception as exc:
                    log.error("slow_tick unhandled exception: %s", exc, exc_info=True)

            # Sleep 1 second at a time for SIGTERM responsiveness
            time.sleep(1)

        log.info("WatcherDaemon: main loop exited")
        self._shutdown()

    def _shutdown(self):
        """Clean up resources on graceful exit."""
        log.info("WatcherDaemon: shutting down")
        try:
            self._journal.close()
        except Exception as exc:
            log.warning("Shutdown: journal close error: %s", exc)
        log.info("WatcherDaemon: shutdown complete")


# ==============================================================================
# SIGNAL HANDLING & ENTRY POINT
# ==============================================================================

def _make_signal_handler(daemon: WatcherDaemon):
    """Return a signal handler that triggers graceful daemon shutdown."""
    def handler(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info("Received signal %s — initiating graceful shutdown", sig_name)
        daemon.stop()
    return handler


if __name__ == "__main__":
    daemon = WatcherDaemon()

    # Register signal handlers for graceful shutdown
    handler = _make_signal_handler(daemon)
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT,  handler)

    try:
        daemon.run()
    except Exception as exc:
        log.critical("WatcherDaemon crashed: %s", exc, exc_info=True)
        sys.exit(1)

    sys.exit(0)
