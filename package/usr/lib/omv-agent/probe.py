"""
OMV Agent Live System Probe — reads from the probe cache written by omv-agent-probe.service.
The cache is refreshed every 5 seconds (fast vitals) and 30 seconds (full probe) by a root
service. This module is read-only — no commands are executed here.
"""
import json
import os
import re
import time

CACHE_PATH = "/run/omv-agent/probe_cache.json"
MAX_CACHE_AGE = 120  # seconds — if cache is older than 2 min, treat as stale


def _load_cache() -> dict | None:
    """Load probe cache. Returns None if missing or stale."""
    try:
        if not os.path.exists(CACHE_PATH):
            return None
        age = time.time() - os.path.getmtime(CACHE_PATH)
        if age > MAX_CACHE_AGE:
            return None
        with open(CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None


def detect_query_type(question: str) -> str | None:
    """
    Detect if a question requires live system data.
    Returns a probe type string or None if the static KB should answer.

    Probe types:
      temperature   — drive temperatures (specific device or all)
      raid          — RAID / md array status
      bcache        — bcache dirty data, writeback on/off, state
      load          — CPU load + memory + uptime
      disk_usage    — df output
      fs_status     — filesystem status (df + lsblk combined)
      drive_health  — all drives: temps + RAID summary
      network       — IP interfaces + routes
      zfs           — ZFS pool status
      lsblk         — block device list
      services      — all running services
      service_status— specific named service status
      updates       — pending apt upgrades
      anomalies     — detected system anomalies / alerts
    """
    q = question.lower()

    # ── CPU / SoC temperature (must come BEFORE generic temperature block) ───
    if any(w in q for w in ("cpu", "processor", "soc", "board temp")):
        if any(w in q for w in ("temp", "hot", "thermal", "°c", "celsius", "heat", "warm")):
            return "temperature"

    # ── SMART / drive health routing ─────────────────────────────────────────
    if "smartctl" in q:
        return "drive_health"
    if "smart" in q and any(w in q for w in ("status", "health", "data", "check", "all", "drives", "disk")):
        return "drive_health"
    if "drive health" in q:
        return "drive_health"

    # ── Drive temperatures ───────────────────────────────────────────────────
    # Specific device path
    if re.search(r'/dev/(nvme|sd|hd|vd)\w+', q):
        if any(w in q for w in ["temp", "temperature", "hot", "heat", "smart",
                                  "health", "status", "°c", "celsius"]):
            return "temperature"
    # "temperature of a drive type"
    if any(w in q for w in ["temperature", "temp", "how hot"]):
        if any(w in q for w in ["drive", "disk", "nvme", "ssd", "hdd", "storage"]):
            return "temperature"
    # "what are the status of nvme" / "nvme status" / "all nvme"
    if "nvme" in q and any(w in q for w in ["status", "state", "health", "all", "what are", "check"]):
        return "temperature"

    # ── RAID / MD arrays ─────────────────────────────────────────────────────
    if any(w in q for w in ["raid", "mdadm", "mdstat", "array"]):
        if any(w in q for w in ["status", "state", "health", "degraded", "rebuild",
                                  "running", "active", "current", "what is", "check",
                                  "raid0", "raid1", "raid5", "raid6", "raid10"]):
            return "raid"
    # "current status of md devices" / "md0 status"
    if re.search(r'\bmd\b', q) and any(w in q for w in [
            "status", "state", "device", "current", "array", "check"]):
        return "raid"
    if re.search(r'\bmd\d+\b', q):
        return "raid"

    # ── bcache ───────────────────────────────────────────────────────────────
    if "bcache" in q:
        return "bcache"
    # "dirty data" / "writeback" / "write-back" queries about cache/disk
    if any(p in q for p in ["dirty data", "dirty_data"]):
        return "bcache"
    if any(w in q for w in ["writeback", "write-back", "write back"]):
        if any(w in q for w in ["bcache", "cache", "disk", "storage", "on", "off"]):
            return "bcache"
    # "is wb on" / "wb off"
    if re.search(r'\bwb\b', q) and any(w in q for w in ["on", "off", "status", "running"]):
        return "bcache"

    # ── System load / resources ──────────────────────────────────────────────
    if any(w in q for w in ["system load", "cpu load", "load average",
                              "load of", "current load"]):
        return "load"
    if "uptime" in q and any(w in q for w in ["current", "how long", "what", "system"]):
        return "load"
    if any(w in q for w in ["memory usage", "ram usage", "free memory",
                              "how much ram", "memory left", "memory status",
                              "ram status", "how much memory"]):
        return "load"

    # ── Disk space ──────────────────────────────────────────────────────────
    if any(w in q for w in ["disk space", "free space", "disk usage",
                              "how much space", "storage space", "how full",
                              "capacity", "disk full"]):
        return "disk_usage"

    # ── Filesystem status ────────────────────────────────────────────────────
    # "status of the file system" / "filesystem status" / "fs status"
    if re.search(r'(file[\s_-]?system|filesys|\bfs\b)', q):
        if any(w in q for w in ["status", "state", "how", "current",
                                  "mounted", "mount", "all", "check"]):
            return "fs_status"

    # ── Drive health (all drives) ────────────────────────────────────────────
    # "are all the drives running in optimal state"
    if any(w in q for w in ["drive", "disk", "drives", "disks", "storage"]):
        if any(w in q for w in ["optimal", "health", "state", "good", "bad",
                                  "overall", "all running", "all drives", "all disks"]):
            return "drive_health"
    if re.search(r'all.*(drive|disk)', q):
        if any(w in q for w in ["running", "status", "ok", "health", "state"]):
            return "drive_health"

    # ── Named service status ─────────────────────────────────────────────────
    # Explicit .service suffix
    if re.search(r'[\w][\w\-\.]*\.service', q):
        return "service_status"
    # Known service names + status verbs
    _svc_names = [
        "smb", "samba", "smbd", "nmbd", "nfs", "nfsd", "nginx", "ssh",
        "docker", "omv-agent", "salt", "collectd", "avahi", "pihole",
        "jellyfin", "monit", "pwm", "fan", "freenove", "darkstat",
        "openmediavault", "engined", "chrony", "cron", "bluetooth",
        "containerd", "uptime-kuma",
    ]
    if any(svc in q for svc in _svc_names):
        if any(w in q for w in ["status", "running", "active", "state", "is",
                                  "started", "enabled", "check", "stopped"]):
            return "service_status"
    # "is X running / active" pattern
    if re.search(r'\b(is|are)\b.+\b(running|active|started|enabled|stopped)\b', q):
        return "service_status"

    # ── Network ─────────────────────────────────────────────────────────────
    if any(w in q for w in ["what is my ip", "current ip", "ip address",
                              "my ip", "what ip", "my network", "network status",
                              "what is my network"]):
        return "network"

    # ── System updates ───────────────────────────────────────────────────────
    if "update" in q and any(w in q for w in [
            "any", "check", "pending", "available", "system", "package"]):
        return "updates"
    if any(p in q for p in ["updates available", "upgrade available",
                              "packages to update", "need to update"]):
        return "updates"

    # ── ZFS ─────────────────────────────────────────────────────────────────
    if any(p in q for p in ["zpool status", "zfs pool status", "zfs status"]):
        return "zfs"

    # ── Block device list ────────────────────────────────────────────────────
    if any(w in q for w in ["list my disks", "list disks", "all disks",
                              "drives attached", "lsblk", "what drives",
                              "what disks", "connected drives"]):
        return "lsblk"

    # ── All running services ─────────────────────────────────────────────────
    if any(p in q for p in ["what services are running", "running services",
                              "services running", "active services", "all services"]):
        return "services"

    # ── Anomalies / system health ────────────────────────────────────────────
    if any(w in q for w in ["anomaly", "anomalies", "alert", "alerts",
                              "problem", "problems", "issue", "issues",
                              "abnormal", "hiccup", "system health"]):
        return "anomalies"

    return None


def _temp_label(temp: int) -> str:
    if temp < 45:  return "Cool ✅"
    if temp < 55:  return "Normal ✅"
    if temp < 65:  return "Warm ⚠️"
    return "HOT ❌"


def _clean_df(df_raw):
    """Filter df output - keep only real storage mounts, skip noise."""
    SKIP_FS = {"tmpfs", "devtmpfs", "udev", "overlay", "squashfs", "none", "cgroupfs"}
    SKIP_MOUNT_PREFIX = ("/proc", "/sys", "/dev/pts", "/run/user", "/snap")
    lines = df_raw.strip().splitlines()
    if not lines:
        return ""
    header = lines[0]
    kept = [header]
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        fs = parts[0].lower()
        mount = parts[-1] if len(parts) >= 6 else ""
        if any(fs.startswith(s) for s in SKIP_FS):
            continue
        if any(mount.startswith(p) for p in SKIP_MOUNT_PREFIX):
            continue
        kept.append(line)
    return "\n".join(kept)


def _fmt_section(title, content):
    """Format a titled section with unicode divider for plain text display."""
    divider = "─" * 48
    return f"{title}\n{divider}\n{content}"


def run_probe(probe_type: str, question: str) -> str | None:
    """Read from probe cache and return a formatted answer."""
    cache = _load_cache()

    if cache is None:
        return (
            "Live data unavailable\n\n"
            "The probe service is starting (takes up to 30 seconds after boot).\n\n"
            "Try again shortly, or check manually:\n"
            "  Load:        cat /proc/loadavg\n"
            "  Drive temps: sudo nvme smart-log /dev/nvme0n1\n"
            "  RAID:        cat /proc/mdstat\n"
            "  Disk usage:  df -h"
        )

    try:
        # ── Temperatures ────────────────────────────────────────────────────
        if probe_type == "temperature":
            # Check if user is asking specifically about CPU
            q_lower = question.lower()
            if any(w in q_lower for w in ("cpu", "processor", "soc", "pi", "board")):
                sys_data = cache.get("system", {})
                cpu_temp = sys_data.get("cpu_temp_c")
                divider = "─" * 40
                if cpu_temp is None:
                    return (
                        f"CPU Temperature  (live)\n{divider}\n"
                        f"? — Could not read thermal sensor\n\n"
                        f"Manual check: cat /sys/class/thermal/thermal_zone0/temp"
                    )
                if cpu_temp >= 80:
                    icon, status = "❌", "CRITICAL — throttling likely"
                elif cpu_temp >= 70:
                    icon, status = "⚠️", "Hot — check cooling"
                elif cpu_temp >= 55:
                    icon, status = "⚠️", "Warm — monitor"
                else:
                    icon, status = "✅", "Normal"
                return (
                    f"CPU Temperature  (live)\n{divider}\n"
                    f"{icon}  {cpu_temp}°C — {status}\n\n"
                    f"────────────────────────────────────────\n"
                    f"Tips\n"
                    f"────────────────────────────────────────\n"
                    f"• Safe range: below 70°C\n"
                    f"• Throttle point: 80°C\n"
                    f"• Pi 5 runs cool with good airflow — idle ~29°C is normal\n"
                    f"• If temp is high: check fan service (freenove-case-pro.service)"
                )

            drives = cache.get("drives", {})
            match = re.search(
                r'/dev/(nvme\d+n?\d*|sd[a-z]+|hd[a-z]+|vd[a-z]+)',
                question, re.IGNORECASE
            )
            if match:
                device = f"/dev/{match.group(1)}"
                data = drives.get(device) or drives.get(device.rstrip("n1").rstrip("n"))
                if data and "temperature_c" in data:
                    temp = data["temperature_c"]
                    return (
                        f"{device} — {temp}°C {_temp_label(temp)}\n\n"
                        f"Source: {data.get('source', 'probe')} | "
                        f"Safe range: NVMe 0-70°C"
                    )
                elif drives:
                    lines = [f"  {d}: {i.get('temperature_c','?')}°C {_temp_label(i.get('temperature_c',0)) if isinstance(i.get('temperature_c'),int) else ''}"
                             for d, i in sorted(drives.items())]
                    divider = "─" * 48
                    return (
                        f"All Drive Temperatures  (live)\n{divider}\n" +
                        "\n".join(lines) +
                        f"\n\n{device} not found in probe data."
                    )
                return f"No temperature data for {device}.\n\nManual: sudo nvme smart-log {device}"
            else:
                # No specific device — show all
                if drives:
                    divider = "─" * 48
                    lines = [
                        f"  {d}: {i.get('temperature_c','?')}°C — {_temp_label(i.get('temperature_c',0)) if isinstance(i.get('temperature_c'),int) else '?'}"
                        for d, i in sorted(drives.items())
                    ]
                    return f"All Drive Temperatures  (live)\n{divider}\n" + "\n".join(lines)
                return "No drive temperature data available."

        # ── RAID / MD arrays ─────────────────────────────────────────────────
        elif probe_type == "raid":
            raid = cache.get("raid", {})
            if raid.get("raid") == "none":
                return "RAID Status\n\nNo software RAID arrays detected."
            arrays = raid.get("raid_arrays", {})
            divider = "─" * 48
            parts = []
            for name, info in arrays.items():
                detail = info.get("detail", "")
                state_m  = re.search(r'State\s*:\s*(.+)',          detail)
                level_m  = re.search(r'Raid Level\s*:\s*(.+)',     detail)
                size_m   = re.search(r'Array Size\s*:\s*(.+)',     detail)
                total_m  = re.search(r'Total Devices\s*:\s*(\d+)', detail)
                active_m = re.search(r'Active Devices\s*:\s*(\d+)',detail)
                failed_m = re.search(r'Failed Devices\s*:\s*(\d+)',detail)

                state  = state_m.group(1).strip()  if state_m  else info.get("status_line", "unknown")
                level  = level_m.group(1).strip()  if level_m  else "unknown"
                size   = size_m.group(1).strip()   if size_m   else "unknown"
                total  = total_m.group(1)          if total_m  else "?"
                active = active_m.group(1)         if active_m else "?"
                failed = failed_m.group(1)         if failed_m else "?"

                ok = "clean" in state.lower() or "active" in state.lower()
                icon = "✅" if ok else "⚠️"
                parts.append(
                    f"{name} {icon}\n"
                    f"  Level:   {level}\n"
                    f"  State:   {state}\n"
                    f"  Size:    {size}\n"
                    f"  Devices: {active}/{total} active, {failed} failed"
                )
            tip = (
                f"\n{divider}\n"
                f"Tips\n"
                f"{divider}\n"
                f"• Degraded RAID: add replacement disk via OMV Storage -> RAID Management\n"
                f"• Check detail: mdadm --detail /dev/md0"
            )
            return f"Live RAID Status\n{divider}\n" + f"\n{divider}\n".join(parts) + tip

        # ── bcache ───────────────────────────────────────────────────────────
        elif probe_type == "bcache":
            bcache = cache.get("bcache", {})
            if bcache.get("_status") == "no bcache devices found":
                return "bcache\n\nNo bcache devices found on this system."
            divider = "─" * 48
            parts = []
            for dev, info in bcache.items():
                if dev == "_status":
                    continue
                wb_on   = info.get("writeback_enabled", False)
                wb_run  = info.get("writeback_running", "?")
                dirty   = info.get("dirty_data", "unknown")
                rate    = info.get("writeback_rate", "N/A")
                pct     = info.get("writeback_percent", "N/A")
                cutoff  = info.get("sequential_cutoff", "N/A")
                mode    = info.get("cache_mode", "unknown")
                state   = info.get("state", "unknown")

                wb_status = "ON ✅" if wb_on else "OFF ⏸"
                parts.append(
                    f"{dev}\n"
                    f"  Writeback:        {wb_status} (running={wb_run})\n"
                    f"  Cache mode:       {mode}\n"
                    f"  State:            {state}\n"
                    f"  Dirty data:       {dirty}\n"
                    f"  Writeback rate:   {rate}\n"
                    f"  Writeback %:      {pct}\n"
                    f"  Sequential cutoff:{cutoff}"
                )
            if not parts:
                return "bcache — No bcache device info available."
            tip = (
                f"\n{divider}\n"
                f"Tips\n"
                f"{divider}\n"
                f"• Toggle writeback: use your bcache-wb script (sudo bcache-wb on/off)\n"
                f"• Dirty data flushes automatically when writeback is disabled"
            )
            return f"bcache Live Status\n{divider}\n" + f"\n{divider}\n".join(parts) + tip

        # ── System load / memory ─────────────────────────────────────────────
        elif probe_type == "load":
            s = cache.get("system", {})
            divider = "─" * 48
            try:
                load = float(s.get("load_1m", "0"))
                load_status = "Normal ✅" if load < 3.0 else ("High ⚠️" if load < 3.8 else "Critical ❌")
            except Exception:
                load_status = ""
            return (
                f"System Status  (live)\n{divider}\n"
                f"Uptime: {s.get('uptime', 'unknown')}\n"
                f"Load:   {s.get('load_1m','?')} (1m) / {s.get('load_5m','?')} (5m) / {s.get('load_15m','?')} (15m) — {load_status}\n\n"
                f"Memory\n{divider}\n{s.get('memory','unavailable')}\n\n"
                f"Pi 5 has 4 cores — load below 4.0 is normal."
            )

        # ── Disk usage ───────────────────────────────────────────────────────
        elif probe_type == "disk_usage":
            disks = cache.get("disks", {})
            divider = "─" * 48
            clean = _clean_df(disks.get('disk_usage', ''))
            return f"Disk Usage  (live)\n{divider}\n{clean}"

        # ── Filesystem status ────────────────────────────────────────────────
        elif probe_type == "fs_status":
            disks = cache.get("disks", {})
            df_raw = disks.get("disk_usage", "")
            lsblk_raw = disks.get("block_devices", "")
            parts = ["Filesystem Status  (live)"]
            divider = "─" * 48
            parts.append(divider)
            if df_raw:
                clean = _clean_df(df_raw)
                parts.append("Mounted Filesystems:\n")
                parts.append(clean)
            if lsblk_raw:
                parts.append("\nBlock Devices:\n")
                parts.append(lsblk_raw)
            parts.append(divider)
            return "\n".join(parts)

        # ── Drive health (all drives) ────────────────────────────────────────
        elif probe_type == "drive_health":
            drives    = cache.get("drives", {})
            raid      = cache.get("raid", {})
            anomalies = cache.get("anomalies", [])

            if not drives:
                return "Drive Health\n\nNo drive data available yet."

            divider = "─" * 48
            all_ok = True
            lines = []
            for dev, info in sorted(drives.items()):
                temp = info.get("temperature_c", "?")
                if isinstance(temp, int):
                    if temp >= 65:
                        all_ok = False
                    # Health indicator based on temperature thresholds
                    if temp < 60:
                        health_icon = "GOOD ✅"
                    elif temp < 75:
                        health_icon = "WARNING ⚠️"
                    else:
                        health_icon = "CRITICAL ❌"
                    lines.append(f"• {dev.ljust(14)}  {str(temp) + '°C':>6}   {health_icon}")
                else:
                    lines.append(f"• {dev.ljust(14)}  {'?':>6}   UNKNOWN ?")

            # RAID summary
            raid_lines = []
            for name, info in raid.get("raid_arrays", {}).items():
                detail = info.get("detail", "")
                st_m = re.search(r'State\s*:\s*(.+)', detail)
                state = st_m.group(1).strip() if st_m else "unknown"
                ok = "clean" in state.lower() or "active" in state.lower()
                if not ok:
                    all_ok = False
                icon = "✅" if ok else "⚠️"
                raid_lines.append(f"  {name}: {state} {icon}")

            summary = "All healthy ✅" if all_ok else "Issues detected ⚠️"
            result = (
                f"Drive Health — {summary}\n{divider}\n"
                f"Temperatures:\n" + "\n".join(lines)
            )
            if raid_lines:
                result += f"\n\nRAID Arrays:\n" + "\n".join(raid_lines)

            drive_alerts = [a for a in anomalies
                            if a.get("type") in ("drive_temp", "raid_degraded", "raid_failed_device")]
            if drive_alerts:
                result += "\n\nActive Alerts:\n" + "\n".join(
                    f"  {a['msg']}" for a in drive_alerts)

            q_lower = question.lower()
            smart_tips = ""
            if "smartctl" in q_lower or "smart" in q_lower or "health" in q_lower:
                smart_tips = (
                    f"\n• Run SMART self-test: smartctl -t short /dev/nvme0n1\n"
                    f"• Full SMART health: smartctl -H /dev/nvmeXn1 → look for PASSED or FAILED\n"
                    f"• All attributes: smartctl -A /dev/nvmeXn1\n"
                    f"• Key indicators: Critical Warning (should be 0x00), Media Errors (should be 0)\n"
                    f"• PASSED = drive healthy. FAILED = replace immediately."
                )

            result += (
                f"\n\n{divider}\n"
                f"Tips\n"
                f"{divider}\n"
                f"• NVMe safe range: 0-70°C normal, >75°C throttle warning\n"
                f"• Check SMART data: smartctl -A /dev/nvmeXn1\n"
                f"• RAID status: cat /proc/mdstat"
                + smart_tips
            )
            return result

        # ── Network ──────────────────────────────────────────────────────────
        elif probe_type == "network":
            net = cache.get("network", {})
            divider = "─" * 48
            ifaces = net.get('interfaces', 'unavailable')
            routes = net.get('routes', 'unavailable')
            return (
                f"Network Interfaces  (live)\n{divider}\n{ifaces}\n\n"
                f"Routes\n{divider}\n{routes}\n\n"
                f"{divider}\n"
                f"Tips\n"
                f"{divider}\n"
                f"• Static IP: OMV -> Network -> Interfaces\n"
                f"• Check connectivity: ping -c 3 8.8.8.8"
            )

        # ── Service status (specific named service) ──────────────────────────
        elif probe_type == "service_status":
            svc_data    = cache.get("services", {})
            statuses    = svc_data.get("statuses", {})
            running_list = svc_data.get("running_services", "")

            # 1. Try to extract exact .service name
            m = re.search(r'([\w][\w\-\.]*[\w])\.service', question, re.IGNORECASE)
            if m:
                raw = m.group(1)
                # strip redundant .service-within-name artefacts
                svc_name = raw.rstrip(".")
            else:
                # 2. Map common aliases to real service names
                _alias = {
                    "smb": "smbd", "samba": "smbd",
                    "nfs": "nfs-kernel-server",
                    "ssh": "ssh",
                    "docker": "docker",
                    "nginx": "nginx",
                    "omv": "openmediavault-engined",
                    "salt": "salt-minion",
                    "openmediavault-pwmfan": "openmediavault-pwmfan",
                    "omv-pwmfan": "openmediavault-pwmfan",
                    "omvpwmfan": "openmediavault-pwmfan",
                    "pwmfan": "openmediavault-pwmfan",
                    "freenove": "freenove-case-pro",
                    "pwm": "freenove-case-pro",
                    "fan": "freenove-case-pro",
                    "pihole": "pihole-FTL",
                    "jellyfin": "jellyfin",
                    "monit": "monit",
                    "collectd": "collectd",
                    "avahi": "avahi-daemon",
                    "darkstat": "darkstat",
                    "chrony": "chrony",
                    "bluetooth": "bluetooth",
                }
                q_lower = question.lower()
                svc_name = None
                for alias, real in _alias.items():
                    if alias in q_lower:
                        svc_name = real
                        break

            # Final alias resolution — runs regardless of how svc_name was obtained
            _canonical = {
                "omv-pwmfan": "openmediavault-pwmfan",
                "omvpwmfan": "openmediavault-pwmfan",
                "pwmfan": "openmediavault-pwmfan",
                "omv-agent-watch": "omv-agent-watch",
                "omv-agent-probe": "omv-agent-probe",
                "omv-agent-discover": "omv-agent-discover",
                "nfs-kernel-server": "nfs-kernel-server",
                "openmediavault-engined": "openmediavault-engined",
            }
            if svc_name and svc_name.lower() in _canonical:
                svc_name = _canonical[svc_name.lower()]

            if svc_name:
                status = statuses.get(svc_name)
                if status is None:
                    # Fall back: search the running-services string
                    if svc_name.lower() in running_list.lower():
                        status = "active"
                    else:
                        status = "inactive"   # not in running list → not running

                icon = "✅" if status == "active" else ("❌" if status == "failed" else "⏸")
                divider = "─" * 40
                return (
                    f"Service: {svc_name}\n{divider}\n"
                    f"{icon}  Status: {status}\n\n"
                    f"Check: systemctl status {svc_name}"
                )
            else:
                # Show all known service statuses
                lines = []
                for svc, st in statuses.items():
                    icon = "✅" if st == "active" else ("❌" if st == "failed" else "⏸")
                    lines.append(f"{icon} {svc}: {st}")
                divider = "─" * 40
                return f"Key Service Statuses\n{divider}\n" + "\n".join(lines)

        # ── System updates ───────────────────────────────────────────────────
        elif probe_type == "updates":
            upd   = cache.get("updates", {})
            count = upd.get("pending_count", -1)
            pkgs  = upd.get("packages", [])
            divider = "─" * 48

            tip = (
                f"\n{divider}\n"
                f"Tips\n"
                f"{divider}\n"
                f"• Apply in OMV: System -> Update Management\n"
                f"• Or via SSH: sudo apt upgrade\n"
                f"• Refresh package list first: sudo apt update"
            )
            if count == -1:
                return (
                    f"System Updates\n{divider}\n"
                    f"Could not check — apt may need a refresh.\n\n"
                    f"Manual: sudo apt update && apt list --upgradable"
                    + tip
                )
            elif count == 0:
                return (
                    f"System Updates\n{divider}\n"
                    f"✅ System is up to date — no pending updates."
                    + tip
                )
            else:
                pkg_list = ", ".join(pkgs[:10])
                more = f" (+{count - 10} more)" if count > 10 else ""
                return (
                    f"System Updates\n{divider}\n"
                    f"⚠️  {count} update{'s' if count != 1 else ''} available\n\n"
                    f"Packages: {pkg_list}{more}"
                    + tip
                )

        # ── ZFS ──────────────────────────────────────────────────────────────
        elif probe_type == "zfs":
            zfs = cache.get("zfs", {})
            divider = "─" * 48
            if zfs.get("zfs") == "none":
                return "ZFS\n\nZFS is not configured on this system."
            return f"ZFS Pool Status\n{divider}\n{zfs.get('zfs_status','unavailable')}"

        # ── Block device list ─────────────────────────────────────────────────
        elif probe_type == "lsblk":
            disks = cache.get("disks", {})
            divider = "─" * 48
            return f"Block Devices\n{divider}\n{disks.get('block_devices','unavailable')}"

        # ── All running services ──────────────────────────────────────────────
        elif probe_type == "services":
            svcs = cache.get("services", {})
            statuses = svcs.get("statuses", {})
            raw = svcs.get("running_services", "")

            # Parse raw systemctl output into clean lines
            # Format: "name.service loaded active running Description"
            parsed = {}
            for line in raw.strip().splitlines():
                parts = line.split()
                if not parts:
                    continue
                name = parts[0]
                if not name.endswith(".service"):
                    continue
                # Extract state from position 2 (active/inactive/failed)
                state = parts[2] if len(parts) > 2 else "unknown"
                parsed[name] = state

            # Merge with statuses dict (more reliable)
            merged = {}
            for svc, st in statuses.items():
                merged[svc + ".service" if not svc.endswith(".service") else svc] = st
            for name, state in parsed.items():
                if name not in merged:
                    merged[name] = state

            if not merged:
                return "No service data available yet."

            lines = []
            for svc in sorted(merged.keys()):
                st = merged[svc]
                if st == "active":
                    icon = "✅"
                elif st == "failed":
                    icon = "❌"
                else:
                    icon = "⏸"
                # Strip .service suffix for cleaner display
                display = svc.replace(".service", "")
                lines.append(f"{icon}  {display}")

            divider = "─" * 48
            tip = (
                f"\n{divider}\n"
                f"Tips\n"
                f"{divider}\n"
                f"• To start a service: systemctl start <name>\n"
                f"• To check logs: journalctl -u <name> -n 50\n"
                f"• To enable on boot: systemctl enable <name>"
            )
            return f"Active Services\n{divider}\n" + "\n".join(lines) + tip

        # ── Anomalies ─────────────────────────────────────────────────────────
        elif probe_type == "anomalies":
            anomalies = cache.get("anomalies", [])
            divider = "─" * 48
            if not anomalies:
                return f"System Health\n{divider}\n✅ No anomalies detected. All systems normal."
            lines = []
            for a in anomalies:
                icon = "❌" if a.get("level") == "critical" else "⚠️"
                lines.append(f"{icon} {a.get('msg', '')}")
            return f"System Anomalies\n{divider}\n" + "\n".join(lines)

    except Exception as e:
        return f"Probe error: {str(e)[:120]}"

    return None
