"""
OMV Agent Brain — Knowledge engine for the OMV Helper.
Handles knowledge retrieval, session deduplication, and relevance filtering.
"""

import sqlite3
import hashlib
import json
import os
import re
import time
import math
import threading
from pathlib import Path

DB_PATH = os.environ.get("OMV_AGENT_DB", "/var/lib/omv-agent/brain.db")
EVENT_QUEUE_PATH = os.environ.get("OMV_AGENT_EVENT_QUEUE", "/run/omv-agent/event_queue.json")
LEARNING_POLL_INTERVAL = int(os.environ.get("OMV_AGENT_LEARNING_INTERVAL", "60"))

# Keywords that confirm a question is OMV/NAS/Linux-related
RELEVANT_KEYWORDS = {
    "omv", "openmediavault", "nas", "disk", "drive", "filesystem", "fs",
    "mount", "fstab", "ext4", "btrfs", "xfs", "zfs", "ntfs", "exfat",
    "share", "samba", "smb", "nfs", "raid", "md", "lvm", "partition",
    "format", "mkfs", "fsck", "smart", "hdparm", "network", "interface",
    "ip", "hostname", "dns", "gateway", "firewall", "iptables", "nftables",
    "nginx", "php", "service", "systemd", "systemctl", "journalctl",
    "permission", "chmod", "chown", "acl", "user", "group", "sudo",
    "plugin", "package", "apt", "dpkg", "docker", "portainer",
    "backup", "rsync", "cron", "schedule", "email", "notification",
    "cpu", "memory", "ram", "temperature", "fan", "ups", "power",
    "ssh", "sftp", "ftp", "ssl", "certificate", "tls", "https",
    "log", "syslog", "dmesg", "storage", "volume", "dataset",
    "snapshot", "quota", "inode", "block", "sector", "bad block",
    "rpc", "workbench", "salt", "engined", "config", "setting",
    "install", "upgrade", "update", "debian", "arm64", "amd64",
    "proc", "sysfs", "devfs", "tmpfs", "overlayfs", "bind mount",
    "scrub", "balance", "subvolume", "compression", "deduplication",
    "pool", "vdev", "zpool", "zfs", "zvol", "arc", "l2arc",
    "mdadm", "degraded", "rebuild", "resync", "spare",
    "bcache", "bcache0", "bcache1", "writeback", "write-back", "writethrough",
    "dirty data", "dirty_data", "cache mode", "wb", "wt", "writearound",
    "update", "upgrade", "pending update", "system update", "apt",
    "disk fail", "disk error", "read error", "write error",
    "wake on lan", "wol", "sleep", "hibernate", "shutdown",
    "collectd", "rrdtool", "monitoring", "graph", "bandwidth",
    # System status / live queries
    "load", "cpu load", "system load", "uptime", "process", "top", "htop",
    "free", "swap", "usage", "utilization", "performance", "resource",
    "speed", "latency", "throughput", "io", "iops", "read speed", "write speed",
    "nvme", "sata", "ata", "hba", "controller", "smart data", "health",
    "array", "parity", "stripe", "mirror", "status", "running", "active",
    "container", "image", "compose", "volume", "port", "mapping",
    "space", "capacity", "used", "available", "free space", "full",
    "slow", "error", "fail", "failed", "broken", "corrupt", "repair",
    "interface", "ethernet", "wifi", "wireless", "speed", "duplex",
    "ping", "route", "subnet", "vlan", "bridge", "bond",
    # Anomaly / health queries
    "anomaly", "anomalies", "abnormal", "abnormality", "abnormalities",
    "alert", "alerts", "detect", "detected", "since boot", "boot",
    "hiccup", "problem", "issue", "issues",
    # Agent self-awareness / capability queries
    "agent", "capability", "capabilities", "feature", "features",
    "what can", "what do you", "help me", "yourself", "about you",
    "version", "changelog",
}

# Triggers that mark a suggestion as a system change (requires warning)
SYSTEM_CHANGE_TRIGGERS = [
    r"\bmkfs\b", r"\bformat\b", r"\bwipe\b", r"\bdd\b\s+if=",
    r"\brm\s+-rf\b", r"\bfdisk\b", r"\bparted\b", r"\bgdisk\b",
    r"\bmdadm\b", r"\bzpool\b.*\bcreate\b", r"\bzpool\b.*\bdestroy\b",
    r"\bfstab\b", r"\bmount\b", r"\bumount\b",
    r"\bsystemctl\b.*\b(restart|stop|start|disable|enable)\b",
    r"\bservice\b.*\b(restart|stop|start)\b",
    r"\bnginx\b.*\b(reload|restart)\b",
    r"\bchmod\b", r"\bchown\b", r"\bsetfacl\b",
    r"\bpasswd\b", r"\busermod\b", r"\buserdel\b",
    r"\biptables\b", r"\bnftables\b",
    r"\bapt\b.*\b(remove|purge|install)\b",
    r"\bdpkg\b.*\b(remove|purge)\b",
    r"\bomv-salt\b", r"\bomv-firstaid\b",
    r"\bsync\b.*\bdisk\b", r"\bblkdiscard\b",
    r"\btune2fs\b", r"\bresize2fs\b",
    r"\bxfs_repair\b", r"\bbtrfs\b.*\b(balance|scrub|device)\b",
    r"\bip\b.*\b(addr|link|route)\b.*\b(add|del|set)\b",
    r"\bnetplan\b.*\bapply\b",
    r"\bdropbear\b", r"\bsshd\b",
]

MAX_SESSION_HISTORY = 50
MAX_QUESTION_LENGTH = 500


class Brain:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # CRIT-4: Harden SQLite connection (FIX-CRIT-4)
        conn.execute("PRAGMA trusted_schema = OFF")   # prevent schema-based attacks
        conn.execute("PRAGMA secure_delete = ON")     # overwrite deleted rows
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # NOTE: conn.enable_load_extension() is NEVER called
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    context_pages TEXT NOT NULL DEFAULT '[]',
                    related_ids TEXT NOT NULL DEFAULT '[]'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
                    USING fts5(id UNINDEXED, title, content, tags, content='knowledge', content_rowid='rowid');
                CREATE TABLE IF NOT EXISTS session_history (
                    session_id TEXT NOT NULL,
                    question_hash TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    PRIMARY KEY (session_id, question_hash)
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    session_id TEXT NOT NULL,
                    question_hash TEXT NOT NULL,
                    helpful INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,
                    PRIMARY KEY (session_id, question_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_session_ts ON session_history(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_knowledge_topic ON knowledge(topic);
            """)

    @staticmethod
    def _knowledge_id(prefix: str, *parts: str) -> str:
        raw = "\x1f".join(str(p or "") for p in parts)
        return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    def _write_knowledge_entry(
        self,
        conn,
        entry_id: str,
        topic: str,
        title: str,
        content: str,
        tags: list,
        context_pages: list | None = None,
        related_ids: list | None = None,
    ) -> bool:
        """
        Insert or update a learned knowledge entry.

        Returns True only when the row was new or changed, so callers can decide
        whether the full-text index needs to be rebuilt.
        """
        content = str(content or "").strip()[:4000]
        if not content:
            return False

        payload = (
            topic,
            title[:160],
            content,
            json.dumps(tags or []),
            json.dumps(context_pages or []),
            json.dumps(related_ids or []),
        )
        existing = conn.execute("""
            SELECT topic, title, content, tags, context_pages, related_ids
            FROM knowledge
            WHERE id = ?
        """, (entry_id,)).fetchone()
        if existing and tuple(existing) == payload:
            return False

        conn.execute("""
            INSERT OR REPLACE INTO knowledge
                (id, topic, title, content, tags, context_pages, related_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (entry_id, *payload))
        return True

    def _read_event_queue(self, event_queue_path: str = EVENT_QUEUE_PATH) -> list:
        """
        Read the local watcher/discoverer event queue.

        watcher.py writes {"events": [...]} while discoverer.py can write a raw
        list, so both shapes are accepted. This is intentionally file-only and
        offline; no network transports are used by the learning bridge.
        """
        try:
            with open(event_queue_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

        if isinstance(data, dict):
            events = data.get("events", [])
        elif isinstance(data, list):
            events = data
        else:
            events = []
        return [e for e in events if isinstance(e, dict)]

    def learn_from_feedback(self, limit: int = 100) -> int:
        """
        Promote helpful feedback-backed answers into the knowledge table.

        The feedback table stores only session_id/question_hash/helpful, so the
        matching session_history row supplies the answer text. Unhelpful feedback
        is read and intentionally not promoted into searchable knowledge.
        """
        changed = 0
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT f.session_id, f.question_hash, f.helpful, f.timestamp, s.answer
                FROM feedback f
                LEFT JOIN session_history s
                  ON s.session_id = f.session_id
                 AND s.question_hash = f.question_hash
                ORDER BY f.timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()

            for row in rows:
                if int(row["helpful"]) != 1 or not row["answer"]:
                    continue
                entry_id = self._knowledge_id(
                    "learned-feedback",
                    row["session_id"],
                    row["question_hash"],
                )
                title = f"Helpful answer learned from feedback {row['question_hash']}"
                content = (
                    "A user marked this OMV Agent answer as helpful.\n\n"
                    f"{row['answer']}"
                )
                if self._write_knowledge_entry(
                    conn,
                    entry_id,
                    "learned-feedback",
                    title,
                    content,
                    ["learned", "feedback", "helpful"],
                ):
                    changed += 1

            if changed:
                conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        return changed

    def learn_from_events(
        self,
        event_queue_path: str = EVENT_QUEUE_PATH,
        limit: int = 100,
    ) -> int:
        """Convert watcher/discoverer events into local system-observation knowledge."""
        events = self._read_event_queue(event_queue_path)[-limit:]
        if not events:
            return 0

        changed = 0
        with self._get_conn() as conn:
            for event in events:
                event_type = str(event.get("type", "event"))[:64]
                source = str(event.get("source", "unknown"))[:128]
                level = str(event.get("level", "info"))[:16]
                msg = str(event.get("msg", "")).strip()[:500]
                timestamp = int(event.get("timestamp", 0) or 0)
                event_id = str(event.get("id", "")).strip()
                if not msg:
                    continue

                entry_id = self._knowledge_id(
                    "learned-event",
                    event_id or event_type,
                    source,
                    msg,
                    str(timestamp),
                )
                title = f"Observed {event_type.replace('_', ' ')} from {source}"
                content = (
                    "Local OMV Agent watcher/discoverer event learned offline.\n\n"
                    f"Level: {level}\n"
                    f"Type: {event_type}\n"
                    f"Source: {source}\n"
                    f"Timestamp: {timestamp}\n"
                    f"Message: {msg}"
                )
                if self._write_knowledge_entry(
                    conn,
                    entry_id,
                    "system-events",
                    title,
                    content,
                    ["learned", "event", level, event_type],
                ):
                    changed += 1

            if changed:
                conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        return changed

    def process_learning(self, event_queue_path: str = EVENT_QUEUE_PATH) -> dict:
        """Run one offline learning pass from feedback and the event queue."""
        feedback_entries = self.learn_from_feedback()
        event_entries = self.learn_from_events(event_queue_path=event_queue_path)
        return {
            "feedback_entries": feedback_entries,
            "event_entries": event_entries,
            "total_entries": feedback_entries + event_entries,
        }

    def start_learning_bridge(
        self,
        event_queue_path: str = EVENT_QUEUE_PATH,
        interval: int = LEARNING_POLL_INTERVAL,
    ):
        """
        Start a lightweight background learner.

        The thread is daemonized so it cannot block service shutdown. It polls
        only local SQLite/file data and writes learned entries to knowledge.
        """
        if getattr(self, "_learning_thread", None):
            if self._learning_thread.is_alive():
                return

        self._learning_stop = threading.Event()

        def _loop():
            while not self._learning_stop.is_set():
                try:
                    self.process_learning(event_queue_path=event_queue_path)
                except Exception:
                    # Learning is opportunistic; query serving must continue.
                    pass
                self._learning_stop.wait(max(5, int(interval)))

        self._learning_thread = threading.Thread(
            target=_loop,
            name="omv-agent-learning-bridge",
            daemon=True,
        )
        self._learning_thread.start()

    def stop_learning_bridge(self):
        stop = getattr(self, "_learning_stop", None)
        if stop:
            stop.set()

    def load_knowledge(self, json_path: str) -> int:
        """Load knowledge_base.json into SQLite. Returns count of entries loaded."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        entries = data.get("entries", [])
        loaded = 0
        with self._get_conn() as conn:
            for entry in entries:
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO knowledge
                            (id, topic, title, content, tags, context_pages, related_ids)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        entry["id"],
                        entry.get("topic", "general"),
                        entry["title"],
                        entry["content"],
                        json.dumps(entry.get("tags", [])),
                        json.dumps(entry.get("context_pages", [])),
                        json.dumps(entry.get("related_ids", [])),
                    ))
                    loaded += 1
                except (KeyError, sqlite3.Error):
                    continue
            # Rebuild FTS index
            conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        return loaded

    def search(self, query: str, context_page: str = None, top_k: int = 3) -> list:
        """Search knowledge base. Returns list of matching entry dicts."""
        query = query.strip()[:MAX_QUESTION_LENGTH]
        if not query:
            return []

        results = []
        with self._get_conn() as conn:
            # FTS search
            try:
                safe_query = re.sub(r'[^\w\s]', ' ', query)
                fts_terms = " OR ".join(
                    f'"{w}"' for w in safe_query.split() if len(w) > 2
                )
                if fts_terms:
                    rows = conn.execute("""
                        SELECT k.id, k.topic, k.title, k.content, k.tags,
                               k.context_pages, k.related_ids,
                               bm25(knowledge_fts) as score
                        FROM knowledge_fts
                        JOIN knowledge k ON k.id = knowledge_fts.id
                        WHERE knowledge_fts MATCH ?
                        ORDER BY score
                        LIMIT ?
                    """, (fts_terms, top_k * 2)).fetchall()
                    results = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                results = []

            # Context page boost: re-rank by page relevance
            if context_page and results:
                for r in results:
                    pages = json.loads(r.get("context_pages", "[]"))
                    if any(context_page.startswith(p) or p.startswith(context_page)
                           for p in pages):
                        r["_boost"] = True

                results.sort(key=lambda r: (0 if r.get("_boost") else 1,
                                            r.get("score", 0)))

        return results[:top_k]

    def is_system_change(self, content: str) -> tuple:
        """
        Returns (bool, warning_message).
        True if content contains system-changing commands/suggestions.
        """
        content_lower = content.lower()
        for pattern in SYSTEM_CHANGE_TRIGGERS:
            if re.search(pattern, content_lower, re.IGNORECASE):
                # Build a human-readable warning
                warning = (
                    "This suggestion involves a system-level change that could "
                    "affect your NAS stability, data, or network connectivity. "
                    "Review carefully before proceeding. "
                    "The helper will NOT execute anything — you must run any commands yourself."
                )
                return True, warning
        return False, ""

    def is_relevant(self, question: str) -> bool:
        """Returns True if question is related to OMV/NAS/Linux topics."""
        q_lower = question.lower()
        # Check for any relevant keyword
        words = set(re.findall(r'\b\w+\b', q_lower))
        if words & RELEVANT_KEYWORDS:
            return True
        # Also allow short questions like "what is fstab?" even if split weirdly
        for kw in RELEVANT_KEYWORDS:
            if kw in q_lower:
                return True
        return False

    @staticmethod
    def hash_question(session_id: str, question: str) -> str:
        """Generate a stable hash for deduplication."""
        normalized = re.sub(r'\s+', ' ', question.strip().lower())
        return hashlib.sha256(f"{session_id}:{normalized}".encode()).hexdigest()[:16]

    def was_already_answered(self, session_id: str, question_hash: str) -> str | None:
        """
        Returns previous answer if this exact question was answered in the last 3
        responses this session, else None. Limits dedup window to avoid rejecting
        legitimate follow-up questions that differ slightly from earlier ones.
        """
        with self._get_conn() as conn:
            # Fetch only the 3 most recent question hashes for this session
            recent = conn.execute("""
                SELECT question_hash, answer FROM session_history
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT 3
            """, (session_id,)).fetchall()
        for row in recent:
            if row["question_hash"] == question_hash:
                return row["answer"]
        return None

    MAX_GLOBAL_HISTORY = 500  # MED-5: SD card exhaustion prevention

    def record_answer(self, session_id: str, question_hash: str, answer: str):
        """Store answered question in session history."""
        with self._get_conn() as conn:
            # Enforce max history per session
            count = conn.execute(
                "SELECT COUNT(*) FROM session_history WHERE session_id = ?",
                (session_id,)
            ).fetchone()[0]

            if count >= MAX_SESSION_HISTORY:
                conn.execute("""
                    DELETE FROM session_history
                    WHERE session_id = ? AND timestamp = (
                        SELECT MIN(timestamp) FROM session_history WHERE session_id = ?
                    )
                """, (session_id, session_id))

            conn.execute("""
                INSERT OR REPLACE INTO session_history
                    (session_id, question_hash, answer, timestamp)
                VALUES (?, ?, ?, ?)
            """, (session_id, question_hash, answer[:4000], int(time.time())))

            # MED-5: Global prune — keep only newest 500 entries across all sessions
            conn.execute("""
                DELETE FROM session_history WHERE rowid NOT IN (
                    SELECT rowid FROM session_history
                    ORDER BY timestamp DESC LIMIT ?
                )
            """, (self.MAX_GLOBAL_HISTORY,))

    def record_feedback(self, session_id: str, question_hash: str, helpful: bool):
        """Store user feedback."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO feedback
                    (session_id, question_hash, helpful, timestamp)
                VALUES (?, ?, ?, ?)
            """, (session_id, question_hash, 1 if helpful else 0, int(time.time())))

    def get_context_entries(self, page: str, limit: int = 5) -> list:
        """Get knowledge entries relevant to a specific OMV page URL."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT id, topic, title, tags, context_pages
                FROM knowledge
                WHERE context_pages LIKE ?
                LIMIT ?
            """, (f"%{page}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return knowledge base statistics."""
        with self._get_conn() as conn:
            kb_count = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            session_count = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM session_history"
            ).fetchone()[0]
        return {"knowledge_entries": kb_count, "active_sessions": session_count}
