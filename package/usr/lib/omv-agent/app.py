#!/usr/bin/env python3
"""
OMV Agent Helper Backend
Flask microservice running on 127.0.0.1:11111
Proxied via nginx at /omv-agent/ for same-origin CSP compliance.
"""

import sys
import os
import json
import re
import secrets
import tempfile
import time
import uuid
import argparse
import logging
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, abort

# Add parent dir to path
sys.path.insert(0, os.path.dirname(__file__))
from brain import Brain
from probe import detect_query_type, run_probe

# ── Config ────────────────────────────────────────────────────────────────────

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("OMV_AGENT_PORT", "11111"))
KNOWLEDGE_JSON = os.environ.get(
    "OMV_AGENT_KNOWLEDGE",
    "/usr/share/omv-agent/knowledge/knowledge_base.json"
)
VERSION = "1.0.0"
MAX_QUESTION_LEN = 500
ALLOWED_CONTENT_TYPE = "application/json"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] omv-agent: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("omv-agent")

# ── App ───────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
brain = Brain()


# ── Middleware ────────────────────────────────────────────────────────────────

@app.before_request
def require_local_proxy():
    """CRIT-1: Reject any request not arriving from nginx on loopback."""
    if request.remote_addr != "127.0.0.1":
        abort(403)


@app.before_request
def enforce_origin():
    """MED-1: Validate Origin header on state-changing requests (CSRF defence)."""
    if request.method in ("POST", "PUT", "PATCH"):
        origin = request.headers.get("Origin", "")
        if origin:
            host = request.headers.get("Host", "").split(":")[0]
            allowed = {f"http://{host}", f"https://{host}"}
            if origin not in allowed:
                abort(403)


def require_json(f):
    """MED-1: Reject requests that aren't application/json (prevents form-based CSRF)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method in ("POST", "PUT", "PATCH"):
            ct = request.content_type or ""
            if ALLOWED_CONTENT_TYPE not in ct:
                return jsonify({"error": "Content-Type must be application/json"}), 415
        return f(*args, **kwargs)
    return decorated


def sanitize_session_id(sid: str) -> str:
    """Validate session ID format — alphanumeric + hyphens only."""
    if not re.match(r'^[a-zA-Z0-9\-]{8,64}$', sid or ""):
        return str(uuid.uuid4())
    return sid


def error_response(message: str, code: int = 400):
    """Generic error — no stack traces, no internal details."""
    return jsonify({"error": message}), code


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    stats = brain.get_stats()
    return jsonify({
        "status": "ok",
        "version": VERSION,
        "knowledge_entries": stats["knowledge_entries"],
    })


@app.route("/query", methods=["POST"])
@require_json
def query():
    """
    Main query endpoint.
    Body: { "question": str, "context_page": str, "session_id": str }
    Returns: { "answer": str, "is_system_change": bool, "warning_message": str,
               "already_answered": bool, "sources": list }
    """
    try:
        data = request.get_json(force=False, silent=True) or {}
    except Exception:
        return error_response("Invalid JSON body")

    question = str(data.get("question", "")).strip()
    context_page = str(data.get("context_page", "")).strip()[:200]
    session_id = sanitize_session_id(data.get("session_id", ""))

    # Validate question (MED-3: byte-length enforcement)
    if not question:
        return error_response("Question cannot be empty")
    if len(question.encode("utf-8")) > 2048:
        return error_response("Question exceeds 2048 byte limit"), 413
    if len(question) > MAX_QUESTION_LEN:
        question = question[:MAX_QUESTION_LEN]

    # Sanitize context_page to valid path-like string
    context_page = re.sub(r'[^a-zA-Z0-9/\-_]', '', context_page)

    # Relevance check
    if not brain.is_relevant(question):
        return jsonify({
            "answer": (
                "I'm specialized in OpenMediaVault, NAS management, and Linux storage. "
                "I can't help with that topic, but I'm happy to answer questions about "
                "your NAS, filesystems, network shares, disk management, or OMV settings."
            ),
            "is_system_change": False,
            "warning_message": "",
            "already_answered": False,
            "sources": [],
        })

    # Deduplication check
    q_hash = Brain.hash_question(session_id, question)
    previous = brain.was_already_answered(session_id, q_hash)
    if previous:
        return jsonify({
            "answer": previous,
            "is_system_change": False,
            "warning_message": "",
            "already_answered": True,
            "sources": [],
        })

    # Live system probe — intercept queries that need real-time data
    probe_type = detect_query_type(question)
    if probe_type:
        probe_answer = run_probe(probe_type, question)
        if probe_answer:
            is_sys_change, warning_msg = brain.is_system_change(probe_answer)
            brain.record_answer(session_id, q_hash, probe_answer)
            return jsonify({
                "answer": probe_answer,
                "is_system_change": is_sys_change,
                "warning_message": warning_msg,
                "already_answered": False,
                "sources": [{"id": "live-probe", "title": "Live System Data", "topic": "system"}],
            })

    # Search knowledge base
    results = brain.search(question, context_page=context_page, top_k=3)

    if not results:
        answer = (
            "I don't have specific information about that in my knowledge base. "
            "For detailed help, check the official OpenMediaVault documentation at "
            "https://docs.openmediavault.org or the OMV community forum."
        )
        sources = []
    else:
        # Build answer from top result(s)
        primary = results[0]
        answer_parts = [f"**{primary['title']}**\n\n{primary['content']}"]

        if len(results) > 1:
            related_titles = [r["title"] for r in results[1:]]
            answer_parts.append(
                "\n\n**Related topics:** " + ", ".join(related_titles)
            )

        answer = "\n".join(answer_parts)
        sources = [{"id": r["id"], "title": r["title"], "topic": r["topic"]}
                   for r in results]

    # System change detection
    is_sys_change, warning_msg = brain.is_system_change(answer)

    # Record in session history
    brain.record_answer(session_id, q_hash, answer)

    return jsonify({
        "answer": answer,
        "is_system_change": is_sys_change,
        "warning_message": warning_msg,
        "already_answered": False,
        "sources": sources,
    })


@app.route("/feedback", methods=["POST"])
@require_json
def feedback():
    """
    Record user feedback on an answer.
    Body: { "session_id": str, "question_hash": str, "helpful": bool }
    """
    try:
        data = request.get_json(force=False, silent=True) or {}
    except Exception:
        return error_response("Invalid JSON body")

    session_id = sanitize_session_id(data.get("session_id", ""))
    q_hash = str(data.get("question_hash", ""))[:32]
    helpful = bool(data.get("helpful", False))

    if not re.match(r'^[a-f0-9]{16}$', q_hash):
        return error_response("Invalid question_hash")

    brain.record_feedback(session_id, q_hash, helpful)
    return jsonify({"status": "ok"})


@app.route("/context/<path:page_path>", methods=["GET"])
def context(page_path):
    """
    Get knowledge entries relevant to a specific OMV page.
    Returns quick-access suggestions for the current panel.
    """
    # Sanitize path
    safe_path = "/" + re.sub(r'[^a-zA-Z0-9/\-_]', '', page_path)
    entries = brain.get_context_entries(safe_path, limit=5)
    return jsonify({
        "page": safe_path,
        "suggestions": [
            {"id": e["id"], "title": e["title"], "topic": e["topic"]}
            for e in entries
        ],
    })


@app.route("/events", methods=["GET"])
def get_events():
    """
    Return events from the watcher daemon event queue.
    Optional query param: since=UNIX_TIMESTAMP (return only events newer than this)
    Returns: { "events": [...], "count": N, "last_updated": T }
    """
    EVENT_QUEUE = "/run/omv-agent/event_queue.json"
    try:
        since = int(request.args.get("since", "0"))
    except (ValueError, TypeError):
        since = 0

    try:
        with open(EVENT_QUEUE, "r") as f:
            data = json.load(f)
        events = data.get("events", [])
        if since > 0:
            events = [e for e in events if e.get("timestamp", 0) > since]
        # Sanitize output — only pass known safe fields
        safe = []
        for e in events[-50:]:  # cap at 50 per response
            safe.append({
                "id":        str(e.get("id", ""))[:32],
                "timestamp": int(e.get("timestamp", 0)),
                "level":     str(e.get("level", "info"))[:16],
                "type":      str(e.get("type", ""))[:64],
                "source":    str(e.get("source", ""))[:128],
                "msg":       str(e.get("msg", ""))[:300],
            })
        return jsonify({
            "events":       safe,
            "count":        len(safe),
            "last_updated": int(data.get("last_updated", 0)),
        })
    except FileNotFoundError:
        return jsonify({"events": [], "count": 0, "last_updated": 0})
    except Exception:
        return jsonify({"events": [], "count": 0, "last_updated": 0})


@app.route("/events", methods=["POST"])
@require_json
def post_event():
    """
    Accept an event from another local OMV plugin and append it to the shared
    event queue.  Only loopback callers are permitted (enforced here in addition
    to the before_request middleware so the contract is explicit).

    Body: { "level": str, "type": str, "source": str, "msg": str }
    Returns: { "ok": true, "id": str } with HTTP 201 on success.
    """
    EVENT_QUEUE = "/run/omv-agent/event_queue.json"

    # Explicit loopback check (defence-in-depth; middleware already enforces this)
    if request.remote_addr != "127.0.0.1":
        abort(403)

    # Parse body
    data = request.get_json(force=False, silent=True)
    if data is None:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    # Required fields
    for field in ("level", "type", "source", "msg"):
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    # Validate level
    level = str(data["level"])
    if level not in ("info", "warning", "critical"):
        return jsonify({"error": "level must be one of: info, warning, critical"}), 400

    # Validate type — alphanumeric, hyphens, underscores, 1-64 chars
    etype = str(data["type"])
    if not re.match(r'^[a-zA-Z0-9_-]{1,64}$', etype):
        return jsonify({"error": "type must match ^[a-zA-Z0-9_-]{1,64}$"}), 400

    # Sanitize source and msg — strip non-printable chars, enforce max length
    def _sanitize(value: str, max_len: int) -> str:
        cleaned = "".join(c for c in str(value) if c.isprintable())
        return cleaned[:max_len]

    source = _sanitize(data["source"], 128)
    msg    = _sanitize(data["msg"],    512)

    # Build event record
    event_id = secrets.token_hex(8)
    new_event = {
        "id":        event_id,
        "timestamp": int(time.time()),
        "level":     level,
        "type":      etype,
        "source":    source,
        "msg":       msg,
    }

    # Atomic queue update
    try:
        # Read current queue
        try:
            with open(EVENT_QUEUE, "r") as f:
                queue_data = json.load(f)
        except FileNotFoundError:
            queue_data = {"events": [], "last_updated": 0}
        except Exception:
            queue_data = {"events": [], "last_updated": 0}

        events = queue_data.get("events", [])
        if not isinstance(events, list):
            events = []

        # Append and enforce ring-buffer limit of 100
        events.append(new_event)
        if len(events) > 100:
            events = events[-100:]

        queue_data["events"] = events
        queue_data["last_updated"] = new_event["timestamp"]

        # Write atomically via tempfile in same directory
        queue_dir = os.path.dirname(EVENT_QUEUE)
        fd, tmp_path = tempfile.mkstemp(dir=queue_dir)
        try:
            os.chmod(tmp_path, 0o644)
            with os.fdopen(fd, "w") as tf:
                json.dump(queue_data, tf)
            os.replace(tmp_path, EVENT_QUEUE)
        except Exception:
            # Clean up temp file if replace failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except Exception:
        log.error("post_event: failed to write event queue", exc_info=True)
        return jsonify({"error": "queue unavailable"}), 503

    return jsonify({"ok": True, "id": event_id}), 201


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_all_errors(e):
    """MED-2: Generic handler — never expose stack traces to client."""
    log.error("Unhandled exception", exc_info=True)
    return jsonify({"error": "An internal error occurred."}), 500


@app.errorhandler(400)
def bad_request(_): return jsonify({"error": "Bad request."}), 400


@app.errorhandler(403)
def forbidden(_): return jsonify({"error": "Forbidden."}), 403


@app.errorhandler(404)
def not_found(_): return jsonify({"error": "Not found."}), 404


@app.errorhandler(405)
def method_not_allowed(_): return jsonify({"error": "Method not allowed."}), 405


@app.errorhandler(413)
def too_large(_): return jsonify({"error": "Request too large."}), 413


@app.errorhandler(415)
def unsupported_media(_): return jsonify({"error": "Content-Type must be application/json."}), 415


@app.errorhandler(429)
def rate_limited(_): return jsonify({"error": "Too many requests."}), 429


# ── Startup ───────────────────────────────────────────────────────────────────

def init_db():
    """Initialize the database from knowledge_base.json."""
    kb_path = Path(KNOWLEDGE_JSON)
    if kb_path.exists():
        log.info("Loading knowledge base from %s", kb_path)
        count = brain.load_knowledge(str(kb_path))
        log.info("Loaded %d knowledge entries", count)
    else:
        log.warning("Knowledge base not found at %s — running with empty brain", kb_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OMV Agent Helper Backend")
    parser.add_argument("--init-db", action="store_true",
                        help="Initialize database and exit")
    args = parser.parse_args()

    init_db()

    if args.init_db:
        log.info("Database initialized. Exiting.")
        sys.exit(0)

    log.info("OMV Agent starting on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
