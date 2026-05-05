#!/usr/bin/env python3
"""
OMV Agent Discovery Daemon — watches for new software, services, and
configurations being added to the system and fires events to the event queue.
"""
import os
import time
import signal
import secrets
import tempfile
import json


# ── SECTION 1: BLUEPRINT CONSTANTS ───────────────────────────────────────────

# Discovery paths to monitor
WATCH_PATHS = {
    "packages":       "/var/lib/dpkg/info/",          # new *.list files = new package installed
    "systemd_system": "/etc/systemd/system/",         # new *.service = new daemon registered
    "systemd_lib":    "/lib/systemd/system/",         # new *.service = system service added
    "nginx_omv":      "/etc/nginx/openmediavault-webgui.d/",  # new nginx config
    "cron_d":         "/etc/cron.d/",                 # new cron jobs
    "cron_daily":     "/etc/cron.daily/",
    "apt_sources":    "/etc/apt/sources.list.d/",     # new apt repos
}

EVENT_QUEUE  = "/run/omv-agent/event_queue.json"
POLL_INTERVAL = 10       # seconds between scans
MAX_EVENTS    = 100      # ring buffer cap (same as watcher.py)
STALE_AFTER   = 3600     # ignore files older than 1 hour on startup (don't flood on first run)


# ── SECTION 2: SNAPSHOT ENGINE ────────────────────────────────────────────────

class Snapshot:
    """Records the current state of each watch path: {watch_key: {filename: mtime}}."""

    def __init__(self):
        self.state = {}
        for key, path in WATCH_PATHS.items():
            self.state[key] = self._scan(path)

    @staticmethod
    def _scan(path):
        """Return {filename: mtime} for all entries in path."""
        result = {}
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        result[entry.name] = entry.stat().st_mtime
                    except OSError:
                        pass
        except OSError:
            pass
        return result

    def diff(self, new_snapshot):
        """Return list of (watch_key, filename, mtime) for files in new but not in self."""
        new_files = []
        for key in WATCH_PATHS:
            old = self.state.get(key, {})
            new = new_snapshot.state.get(key, {})
            for filename, mtime in new.items():
                if filename not in old:
                    new_files.append((key, filename, mtime))
        return new_files


# ── SECTION 3: EVENT QUEUE WRITER ─────────────────────────────────────────────

def write_event(level, event_type, source, message):
    """
    Append a new event to EVENT_QUEUE.
    Schema: {"id": hex16, "timestamp": int, "level": str, "type": str, "source": str, "msg": str}
    Enforces MAX_EVENTS ring buffer. Writes atomically via tempfile + os.replace.
    """
    event = {
        "id":        secrets.token_hex(8),
        "timestamp": int(time.time()),
        "level":     level,
        "type":      event_type,
        "source":    source,
        "msg":       message,
    }

    # Read existing queue
    try:
        with open(EVENT_QUEUE, "r") as f:
            queue = json.load(f)
        if not isinstance(queue, list):
            queue = []
    except FileNotFoundError:
        queue = []
    except Exception:
        queue = []

    queue.append(event)

    # Enforce ring buffer cap
    if len(queue) > MAX_EVENTS:
        queue = queue[-MAX_EVENTS:]

    # Atomic write via tempfile
    dir_path = os.path.dirname(EVENT_QUEUE)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(queue, f)
        os.replace(tmp_path, EVENT_QUEUE)
        os.chmod(EVENT_QUEUE, 0o644)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── SECTION 4: CLASSIFIER ─────────────────────────────────────────────────────

def classify_new_file(watch_key, filename):
    """
    Return (level, event_type, message) for a newly detected file.
    """
    if watch_key == "packages" and filename.endswith(".list"):
        pkg_name = filename[:-5]  # strip .list suffix
        return (
            "info",
            "package_installed",
            f"New package installed: {pkg_name} — check service status and any new daemons it may have added",
        )

    if watch_key in ("systemd_system", "systemd_lib") and filename.endswith(".service"):
        return (
            "info",
            "service_registered",
            f"New systemd service registered: {filename} — use 'systemctl status {filename}' to check its state, or ask the assistant",
        )

    if watch_key == "nginx_omv":
        return (
            "info",
            "nginx_config_added",
            f"New nginx config added to OMV webgui: {filename} — nginx reload may be required",
        )

    if watch_key in ("cron_d", "cron_daily"):
        return (
            "info",
            "cron_added",
            f"New scheduled task registered: {filename} — review with 'crontab -l' or check /etc/cron.d/",
        )

    if watch_key == "apt_sources":
        return (
            "warning",
            "apt_repo_added",
            f"New apt repository added: {filename} — verify the source is trusted before running apt update",
        )

    # Fallback
    return (
        "info",
        "fs_change",
        f"New file detected in monitored path: {watch_key}/{filename}",
    )


# ── SECTION 5: MAIN DAEMON LOOP ───────────────────────────────────────────────

class DiscovererDaemon:
    def __init__(self):
        self.startup_time = time.time()
        self.last_snapshot = Snapshot()
        self.running = True

    def _handle_signal(self, sig, frame):
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        print("omv-agent-discover: started", flush=True)

        while self.running:
            try:
                new_snapshot = Snapshot()
                new_files = self.last_snapshot.diff(new_snapshot)

                for watch_key, filename, mtime in new_files:
                    # Skip files that pre-date our startup (don't flood on first run)
                    if mtime <= self.startup_time:
                        continue

                    level, event_type, message = classify_new_file(watch_key, filename)
                    write_event(level, event_type, "discoverer", message)
                    print(
                        f"omv-agent-discover: [{level}] {event_type} — {filename}",
                        file=__import__("sys").stderr,
                        flush=True,
                    )

                self.last_snapshot = new_snapshot

            except Exception as exc:
                print(
                    f"omv-agent-discover: error in main loop: {exc}",
                    file=__import__("sys").stderr,
                    flush=True,
                )

            # Sleep in 1s increments so SIGTERM is responsive
            for _ in range(POLL_INTERVAL):
                if not self.running:
                    break
                time.sleep(1)

        print("omv-agent-discover: stopped", flush=True)


if __name__ == "__main__":
    import sys
    daemon = DiscovererDaemon()
    daemon.run()
