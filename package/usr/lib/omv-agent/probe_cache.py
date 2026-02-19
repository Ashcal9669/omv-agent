#!/usr/bin/env python3
"""
OMV Agent Probe Cache — runs as root, writes live system data.

Fast cycle  (every 5s):  vitals + anomaly detection (load, mem, disk, services)
Full cycle  (every 30s): everything above + temperatures, RAID, bcache, updates

The main omv-agent service (www-data) reads /run/omv-agent/probe_cache.json.
This separation keeps the main service fully hardened (NoNewPrivileges=yes).
"""
import subprocess
import json
import os
import re
import time
import signal
import sys
import glob as _glob

CACHE_PATH = "/run/omv-agent/probe_cache.json"
INTERVAL_FAST = 5    # seconds — fast vitals cycle
INTERVAL_FULL = 30   # seconds — full probe cycle (must be a multiple of INTERVAL_FAST)
TIMEOUT = 5          # subprocess timeout for fast commands
TIMEOUT_SLOW = 8     # subprocess timeout for slow commands (nvme, mdadm)

running = True


def _run(cmd: list, timeout: int = TIMEOUT) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
        )
        return r.stdout + r.stderr
    except Exception:
        return ""


# ── Fast probes (every 5s) ───────────────────────────────────────────────────

def _read_rrd_last(rrd_path, ds_name, daemon_sock="unix:/run/rrdcached.sock"):
    """Read last value for a DS from an RRD file via rrdtool subprocess. Returns float or None."""
    try:
        result = subprocess.run(
            ['rrdtool', 'lastupdate', rrd_path, '--daemon', daemon_sock],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return None
        ds_names = lines[0].strip().split()
        parts = lines[1].strip().split(': ', 1)
        values = parts[1].split() if len(parts) > 1 else []
        data = {name: (float(val) if val not in ('nan', 'NaN') else None)
                for name, val in zip(ds_names, values)}
        return data.get(ds_name)
    except Exception:
        return None


def probe_system() -> dict:
    # Try RRD for load averages first (collectd writes here on OMV 8)
    RRD_LOAD = "/var/lib/rrdcached/db/localhost/load/load.rrd"
    load_1m  = _read_rrd_last(RRD_LOAD, "shortterm")
    load_5m  = _read_rrd_last(RRD_LOAD, "midterm")
    load_15m = _read_rrd_last(RRD_LOAD, "longterm")

    if load_1m is None or load_5m is None or load_15m is None:
        # Fall back to /proc/loadavg
        load = _run(["cat", "/proc/loadavg"]).split()
        load_1m_str  = load[0] if len(load) > 0 else "?"
        load_5m_str  = load[1] if len(load) > 1 else "?"
        load_15m_str = load[2] if len(load) > 2 else "?"
    else:
        load_1m_str  = f"{load_1m:.2f}"
        load_5m_str  = f"{load_5m:.2f}"
        load_15m_str = f"{load_15m:.2f}"

    mem = _run(["free", "-h"])
    uptime = _run(["uptime", "-p"]).strip()

    # CPU temperature (Raspberry Pi / ARM SoC)
    cpu_temp_c = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            cpu_temp_c = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass

    return {
        "load_1m":  load_1m_str,
        "load_5m":  load_5m_str,
        "load_15m": load_15m_str,
        "memory": mem.strip(),
        "uptime": uptime,
        "cpu_temp_c": cpu_temp_c,
    }


def probe_network() -> dict:
    interfaces = _run(["ip", "-br", "addr"]).strip()
    routes = _run(["ip", "route"]).strip()[:500]
    return {"interfaces": interfaces, "routes": routes}


def probe_disks() -> dict:
    df = _run(["df", "-h", "--output=source,size,used,avail,pcent,target"]).strip()
    lsblk = _run(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL"]).strip()
    return {"disk_usage": df, "block_devices": lsblk}


def probe_services() -> dict:
    """Check individual service statuses + running list."""
    key_services = [
        "smbd", "nmbd", "nfs-kernel-server", "nginx", "ssh",
        "docker", "omv-agent", "omv-agent-probe",
        "openmediavault-engined", "salt-minion", "collectd",
        "avahi-daemon", "pihole-FTL", "jellyfin", "monit",
        "darkstat", "chrony", "bluetooth", "cron",
    ]
    statuses = {}
    for svc in key_services:
        out = _run(["systemctl", "is-active", svc]).strip()
        statuses[svc] = out  # "active", "inactive", "failed", "unknown"

    running_list = _run([
        "systemctl", "list-units", "--type=service", "--state=running",
        "--no-pager", "--no-legend"
    ]).strip()[:1500]

    return {"statuses": statuses, "running_services": running_list}


# ── Slow probes (every 30s) ──────────────────────────────────────────────────

def probe_drives() -> dict:
    """Probe temperatures for all block devices."""
    temps = {}
    lsblk = _run(["lsblk", "-dn", "-o", "NAME,TYPE"])
    for line in lsblk.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "disk":
            continue
        dev = f"/dev/{parts[0]}"
        if "nvme" in parts[0]:
            out = _run(["nvme", "smart-log", dev], timeout=TIMEOUT_SLOW)
            m = re.search(r'temperature\s*:\s*(\d+)', out, re.IGNORECASE)
            if m:
                temps[dev] = {"temperature_c": int(m.group(1)), "source": "nvme-cli"}
                continue
        # SMART fallback for SATA/SAS
        out = _run(["smartctl", "-A", "-j", dev], timeout=TIMEOUT_SLOW)
        try:
            data = json.loads(out)
            temp = data.get("temperature", {}).get("current")
            if temp:
                temps[dev] = {"temperature_c": temp, "source": "smartctl"}
                continue
        except Exception:
            pass
    return temps


def probe_raid() -> dict:
    out = _run(["cat", "/proc/mdstat"])
    if "Personalities" not in out:
        return {"raid": "none"}
    arrays = {}
    current = None
    for line in out.splitlines():
        if re.match(r'^md\d+', line):
            current = line.split()[0]
            arrays[current] = {"status_line": line.strip()}
        elif current and ("blocks" in line or "[" in line):
            arrays[current]["blocks_line"] = line.strip()
    for name in list(arrays.keys()):
        detail = _run(["mdadm", "--detail", f"/dev/{name}"], timeout=TIMEOUT_SLOW)
        if detail:
            arrays[name]["detail"] = detail[:900]
    return {"raid_arrays": arrays, "mdstat": out.strip()}


def probe_bcache() -> dict:
    """Read bcache device stats: dirty data, cache mode, state, writeback."""
    result = {}
    try:
        paths = _glob.glob("/sys/block/bcache*/bcache")
        for path in paths:
            dev = os.path.basename(os.path.dirname(path))
            info = {}
            for attr in ["dirty_data", "cache_mode", "state",
                         "writeback_running", "writeback_rate",
                         "writeback_percent", "sequential_cutoff"]:
                try:
                    info[attr] = open(f"{path}/{attr}").read().strip()
                except Exception:
                    pass
            # writeback_running=1 means WB is active; =0 means off
            wb_run = info.get("writeback_running", "")
            info["writeback_enabled"] = wb_run == "1"
            result[dev] = info
    except Exception:
        pass
    if not result:
        result["_status"] = "no bcache devices found"
    return result


def probe_zfs() -> dict:
    out = _run(["zpool", "status"])
    if not out.strip():
        return {"zfs": "none"}
    return {"zfs_status": out.strip()}


def probe_updates() -> dict:
    """Check for pending apt upgrades (reads local cache — no network needed)."""
    try:
        out = _run(["apt", "list", "--upgradable"], timeout=10)
        packages = []
        for line in out.splitlines():
            if "/" in line and line.strip() and not line.startswith("Listing"):
                pkg = line.split("/")[0].strip()
                if pkg:
                    packages.append(pkg)
        return {
            "pending_count": len(packages),
            "packages": packages[:20],
            "checked_at": int(time.time()),
        }
    except Exception:
        return {"pending_count": -1, "packages": [], "checked_at": int(time.time())}


# ── Anomaly detection ────────────────────────────────────────────────────────

def detect_anomalies(system: dict, drives: dict, raid: dict,
                     disks: dict, services: dict) -> list:
    """Return list of anomaly dicts: {level, type, msg}."""
    alerts = []

    # High CPU load (Pi 5 has 4 cores)
    try:
        load = float(system.get("load_1m", "0"))
        if load > 3.8:
            alerts.append({
                "level": "critical", "type": "high_load",
                "msg": f"CPU load critical: {load:.2f} (4-core Pi — >3.8 = overloaded)"
            })
        elif load > 3.0:
            alerts.append({
                "level": "warning", "type": "high_load",
                "msg": f"CPU load high: {load:.2f} (4-core Pi — consider checking processes)"
            })
    except Exception:
        pass

    # Drive temperatures
    for dev, info in drives.items():
        temp = info.get("temperature_c", 0)
        if isinstance(temp, int):
            if temp >= 70:
                alerts.append({
                    "level": "critical", "type": "drive_temp",
                    "msg": f"{dev} CRITICAL: {temp}°C — check airflow immediately"
                })
            elif temp >= 60:
                alerts.append({
                    "level": "warning", "type": "drive_temp",
                    "msg": f"{dev} warm: {temp}°C — monitor closely"
                })

    # RAID health
    for name, info in raid.get("raid_arrays", {}).items():
        detail = info.get("detail", "")
        if "degraded" in detail.lower():
            alerts.append({
                "level": "critical", "type": "raid_degraded",
                "msg": f"RAID {name} is DEGRADED — check mdadm immediately"
            })
        failed_m = re.search(r'Failed Devices\s*:\s*([1-9]\d*)', detail)
        if failed_m:
            alerts.append({
                "level": "critical", "type": "raid_failed_device",
                "msg": f"RAID {name} has {failed_m.group(1)} failed device(s)"
            })

    # Disk usage > 85%
    for line in disks.get("disk_usage", "").splitlines():
        m = re.search(r'(\d+)%\s+(\S+)$', line)
        if m:
            pct = int(m.group(1))
            mount = m.group(2)
            if pct >= 95:
                alerts.append({
                    "level": "critical", "type": "disk_full",
                    "msg": f"Disk {mount} is {pct}% full — nearly full!"
                })
            elif pct >= 85:
                alerts.append({
                    "level": "warning", "type": "disk_full",
                    "msg": f"Disk {mount} is {pct}% full"
                })

    # Critical service failures
    critical_svcs = ["nginx", "openmediavault-engined", "omv-agent"]
    for svc in critical_svcs:
        status = services.get("statuses", {}).get(svc, "")
        if status in ("failed", "inactive"):
            alerts.append({
                "level": "warning", "type": "service_down",
                "msg": f"Service '{svc}' is {status}"
            })

    return alerts


# ── Collection tiers ─────────────────────────────────────────────────────────

def collect_fast(last_full: dict) -> dict:
    """Fast vitals + anomaly detection. Reuses cached slow data."""
    system   = probe_system()
    disks    = probe_disks()
    network  = probe_network()
    services = probe_services()

    # Reuse slow data from last full cycle
    drives = last_full.get("drives", {})
    raid   = last_full.get("raid",   {"raid": "none"})
    bcache = last_full.get("bcache", {})
    zfs    = last_full.get("zfs",    {"zfs": "none"})
    updates = last_full.get("updates", {"pending_count": -1, "packages": []})

    anomalies = detect_anomalies(system, drives, raid, disks, services)

    return {
        "timestamp": int(time.time()),
        "drives":    drives,
        "raid":      raid,
        "system":    system,
        "network":   network,
        "disks":     disks,
        "bcache":    bcache,
        "zfs":       zfs,
        "services":  services,
        "updates":   updates,
        "anomalies": anomalies,
    }


def collect_full() -> dict:
    """Full probe — all data including slow probes."""
    drives   = probe_drives()
    raid     = probe_raid()
    system   = probe_system()
    network  = probe_network()
    disks    = probe_disks()
    bcache   = probe_bcache()
    zfs      = probe_zfs()
    services = probe_services()
    updates  = probe_updates()
    anomalies = detect_anomalies(system, drives, raid, disks, services)

    return {
        "timestamp": int(time.time()),
        "drives":    drives,
        "raid":      raid,
        "system":    system,
        "network":   network,
        "disks":     disks,
        "bcache":    bcache,
        "zfs":       zfs,
        "services":  services,
        "updates":   updates,
        "anomalies": anomalies,
    }


def write_cache(data: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE_PATH)
    os.chmod(CACHE_PATH, 0o644)


# ── Signal handling ──────────────────────────────────────────────────────────

def handle_signal(sig, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ── Main loop ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    fast_tick = 0
    full_every = INTERVAL_FULL // INTERVAL_FAST  # e.g. 30/5 = 6

    last_full_data: dict = {}

    while running:
        try:
            fast_tick += 1
            if fast_tick == 1 or (fast_tick % full_every) == 0:
                # Full cycle on first run, then every 30s
                data = collect_full()
                last_full_data = data
            else:
                # Fast cycle — vitals only, reuse cached slow data
                data = collect_fast(last_full_data)
            write_cache(data)
        except Exception:
            pass

        # Sleep in 1s increments so SIGTERM is responsive
        for _ in range(INTERVAL_FAST):
            if not running:
                break
            time.sleep(1)
