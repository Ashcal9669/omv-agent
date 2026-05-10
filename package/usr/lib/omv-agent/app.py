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
import threading
import time
import uuid
import argparse
import logging
from collections import deque
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, abort

# Add parent dir to path
sys.path.insert(0, os.path.dirname(__file__))
from brain import Brain
from ollama_bridge import answer_question, interpret_question
from probe import detect_query_type, run_probe

# ── Config ────────────────────────────────────────────────────────────────────

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("OMV_AGENT_PORT", "11111"))
KNOWLEDGE_JSON = os.environ.get(
    "OMV_AGENT_KNOWLEDGE",
    "/usr/share/omv-agent/knowledge/knowledge_base.json"
)
VERSION = "1.7.2"
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

# ── Session context (in-memory, per browser tab) ──────────────────────────────
# Each session stores the last N (question, answer) turns so follow-up questions
# like "why?", "what about nvme1n1?", "explain that" can be routed correctly.

_session_ctx: dict = {}
_session_ctx_lock = threading.Lock()
_MAX_CTX_TURNS = 5

# Phrases that are TRUE anaphoric follow-ups (reference a specific prior answer)
# Keep this list narrow — broad phrases incorrectly enrich standalone questions
_FOLLOWUP_SIGNALS = frozenset({
    "what about", "how about", "what does that", "what did you mean",
    "explain that", "same for", "compared to", "the other one",
    "tell me more", "more detail", "more info about that",
    "what is that", "what are those", "what does it mean",
    "does it affect", "is it related", "are they the same",
    "which one is", "what was that", "and that one",
})


def _get_ctx(session_id: str) -> list:
    """Return recent (question, answer) turns for this session."""
    with _session_ctx_lock:
        return list(_session_ctx.get(session_id, []))


def _store_ctx(session_id: str, q: str, a: str):
    """Store a new turn in the in-memory session context."""
    with _session_ctx_lock:
        if session_id not in _session_ctx:
            _session_ctx[session_id] = deque(maxlen=_MAX_CTX_TURNS)
        _session_ctx[session_id].append((q, a))


def _enrich(question: str, ctx: list) -> str:
    """
    Enrich a question with prior session context ONLY when it is a true follow-up:
    - A bare one-or-two-word question (after stripping punctuation) like "why?" or "how so?"
    - OR contains an explicit anaphoric phrase from _FOLLOWUP_SIGNALS

    Standalone questions — even short ones like "system loads?" or "any anomalies?" —
    are NOT enriched, so they route correctly on their own keywords.
    """
    if not ctx:
        return question
    q_lower = question.lower().strip()
    q_stripped = q_lower.rstrip("?!. ").strip()
    word_count = len(q_stripped.split())

    is_followup = (
        # True bare follow-ups: single word or two-word anaphors
        word_count <= 2 and q_stripped in (
            "why", "how", "what", "and", "ok", "really", "continue", "more",
            "else", "explain", "why not", "how so", "what then", "and then",
            "so what", "like what", "for example", "such as",
        )
        # Explicit anaphoric signal phrases
        or any(sig in q_lower for sig in _FOLLOWUP_SIGNALS)
    )

    if not is_followup:
        return question

    # Prepend up to the last 2 prior questions as context hints
    ctx_qs = [t[0] for t in ctx[-2:]]
    return ". ".join(ctx_qs) + ". " + question


def _is_agent_alert_query(question: str) -> bool:
    """
    Detect direct questions about the OMV Agent's own warnings, badge, or alerts.

    These are always in-scope and should route to the anomalies/event path even
    if the broader relevance gate finds the phrasing too human or indirect.
    """
    q = str(question or "").lower().strip()
    if not q:
        return False

    alert_terms = (
        "warning", "warnings", "warn", "warns",
        "alert", "alerts", "anomaly", "anomalies",
        "badge", "notification", "notifications",
        "event", "events", "issue", "issues",
    )
    agent_terms = (
        "omv-agent", "omv agent", "agent", "helper",
        "you keep giving", "you are giving", "you keep showing",
        "where is", "where are", "what are", "what is",
        "show me", "tell me", "why are you", "why do you",
    )

    return any(a in q for a in alert_terms) and any(t in q for t in agent_terms)


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


@app.route("/boot-status", methods=["GET"])
def boot_status():
    report_file = "/run/omv-agent/boot_report.json"
    if not os.path.exists(report_file):
        return jsonify({"status": "pending"}), 200

    try:
        with open(report_file, "r") as f:
            return jsonify(json.load(f)), 200
    except Exception:
        log.error("boot_status: failed to read boot report", exc_info=True)
        return jsonify({"status": "pending"}), 200


@app.route("/query", methods=["POST"])
@require_json
def query():
    """
    Main query endpoint.
    """
    try:
        data = request.get_json(force=False, silent=True) or {}
    except Exception:
        return error_response("Invalid JSON body")

    question = str(data.get("question", "")).strip()
    context_page = str(data.get("context_page", "")).strip()[:200]
    session_id = sanitize_session_id(data.get("session_id", ""))

    if not question:
        return error_response("Question cannot be empty")

    ctx = _get_ctx(session_id)
    enriched_q = _enrich(question, ctx)
    agent_alert_query = _is_agent_alert_query(question) or _is_agent_alert_query(enriched_q)

    q_hash = Brain.hash_question(session_id, question)
    previous = brain.was_already_answered(session_id, q_hash)
    if previous:
        return jsonify({"answer": previous, "is_system_change": False, "warning_message": "", "already_answered": True, "sources": []})

    ollama_interpretation = None
    stored_ollama_answer = None

    if not brain.is_relevant(enriched_q) and not agent_alert_query:
        ollama_interpretation = interpret_question(question, context_page=context_page)
        if ollama_interpretation:
            stored_ollama_answer = ollama_interpretation.get("answer")
            if ollama_interpretation.get("in_scope"):
                rewritten_q = str(ollama_interpretation.get("rewritten_question", "")).strip()
                enriched_q = _enrich(rewritten_q or question, ctx)
                if not brain.is_relevant(enriched_q) and not detect_query_type(enriched_q):
                    ans = str(stored_ollama_answer).strip() or "I understand this as OMV related but have no local data."
                    return jsonify({"answer": ans, "is_system_change": False, "warning_message": "", "already_answered": False, "sources": []})
            else:
                ans = str(stored_ollama_answer).strip() or "I specialize in OMV, NAS and Linux storage."
                return jsonify({"answer": ans, "is_system_change": False, "warning_message": "", "already_answered": False, "sources": []})

    else:
        probe_type = "anomalies" if agent_alert_query else detect_query_type(enriched_q)
    if probe_type:
        probe_answer = run_probe(probe_type, question)
        if probe_answer:
            brain.record_answer(session_id, q_hash, probe_answer)
            _store_ctx(session_id, question, probe_answer)
            return jsonify({"answer": probe_answer, "is_system_change": False, "warning_message": "", "already_answered": False, "sources": [{"id": "live-probe", "title": "Live Data", "topic": "system"}]})

    results = brain.search(enriched_q, context_page=context_page, top_k=3)
    confident = results if results and brain.is_confident_match(enriched_q, results[0]) else []

    if not confident:
        ans = str(stored_ollama_answer).strip() if stored_ollama_answer else None
        if not ans or "specialized" in ans.lower():
            ans = answer_question(enriched_q, context_page=context_page)
        if not ans:
            ans = "I do not have specific info in my knowledge base."
        answer, sources = ans, []
    else:
        primary = confident[0]
        answer = f"**{primary['title']}**\n\n{primary['content']}"
        sources = [{"id": r['id'], "title": r['title'], "topic": r['topic']} for r in confident]

    is_sys, warn = brain.is_system_change(answer)
    brain.record_answer(session_id, q_hash, answer)
    _store_ctx(session_id, question, answer)
    return jsonify({"answer": answer, "is_system_change": is_sys, "warning_message": warn, "already_answered": False, "sources": sources})

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
    try:
        brain.process_learning()
    except Exception:
        log.warning("feedback: learning bridge pass failed", exc_info=True)
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
    brain.start_learning_bridge()


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
