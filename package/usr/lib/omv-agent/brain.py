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
from pathlib import Path

DB_PATH = os.environ.get("OMV_AGENT_DB", "/var/lib/omv-agent/brain.db")

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
        """Returns previous answer if already answered this session, else None."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT answer FROM session_history
                WHERE session_id = ? AND question_hash = ?
            """, (session_id, question_hash)).fetchone()
        return row["answer"] if row else None

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
