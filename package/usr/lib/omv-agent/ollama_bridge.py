"""
Optional local Ollama bridge for human-phrasing fallback.

This module only talks to loopback Ollama endpoints. It never calls external
hosts and it is intentionally best-effort: failures return None so the normal
OMV Agent refusal path can continue.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


OLLAMA_URL = os.environ.get("OMV_AGENT_OLLAMA_URL", "http://127.0.0.1:11334")
OLLAMA_MODEL = os.environ.get("OMV_AGENT_OLLAMA_MODEL", "")
OLLAMA_TIMEOUT = float(os.environ.get("OMV_AGENT_OLLAMA_TIMEOUT", "45"))
OLLAMA_ENABLED = os.environ.get("OMV_AGENT_OLLAMA_ENABLED", "0").lower() in {
    "1", "true", "yes", "on"
}
MAX_PROMPT_QUESTION_LEN = 500
MAX_ANSWER_LEN = 1200


def _is_loopback_url(url: str) -> bool:
    """Allow only local Ollama HTTP endpoints."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _ollama_generate(prompt: str) -> str | None:
    if not OLLAMA_ENABLED:
        return None
    if not _is_loopback_url(OLLAMA_URL):
        return None

    model = _select_model()
    if not model:
        return None

    endpoint = OLLAMA_URL.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1024,
        },
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read(64 * 1024).decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    text = str(data.get("response", "")).strip()
    return text or None


def _select_model() -> str:
    """Use configured model, otherwise pick the first locally installed Qwen model."""
    if OLLAMA_MODEL.strip():
        return OLLAMA_MODEL.strip()
    if not _is_loopback_url(OLLAMA_URL):
        return ""

    endpoint = OLLAMA_URL.rstrip("/") + "/api/tags"
    req = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            if resp.status != 200:
                return ""
            data = json.loads(resp.read(64 * 1024).decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return ""

    models = data.get("models", [])
    names = [
        str(m.get("name", "")).strip()
        for m in models
        if isinstance(m, dict) and str(m.get("name", "")).strip()
    ]
    for name in names:
        if "qwen" in name.lower():
            return name
    return names[0] if names else ""


def _extract_json(text: str) -> dict | None:
    """Parse JSON directly, or from the first {...} block if the model adds text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def interpret_question(question: str, context_page: str = "") -> dict | None:
    """
    Ask local Ollama to translate ambiguous wording into an OMV Agent query.

    Returns:
      {"in_scope": bool, "rewritten_question": str, "answer": str}
    """
    question = str(question or "").strip()[:MAX_PROMPT_QUESTION_LEN]
    context_page = str(context_page or "").strip()[:200]
    if not question:
        return None

    prompt = f"""You are a local-only router for an OpenMediaVault NAS assistant.
No internet access is available. Do not claim to browse or fetch remote data.
Do not execute commands or suggest that you executed commands.

Decide whether the user's question is about OpenMediaVault, NAS administration,
Linux storage, filesystems, disks, RAID, bcache, SMART, services, system load,
memory, network, updates, logs, Docker on the NAS, or the OMV Agent itself.

If it is in scope, rewrite it as a clear query the existing OMV Agent can route.
Prefer these live query phrasings when appropriate:
- "current system load and memory usage"
- "current system health anomalies"
- "disk usage"
- "filesystem status"
- "drive health"
- "network status"
- "running services"
- "pending system updates"
- "agent capabilities"

If it is out of scope, set in_scope false and give a short refusal.
Return JSON only with keys: in_scope, rewritten_question, answer.

Context page: {context_page}
User question: {question}
"""
    raw = _ollama_generate(prompt)
    if not raw:
        return None

    data = _extract_json(raw)
    if not data:
        return None

    in_scope = bool(data.get("in_scope", False))
    rewritten = str(data.get("rewritten_question", "")).strip()[:MAX_PROMPT_QUESTION_LEN]
    answer = str(data.get("answer", "")).strip()[:MAX_ANSWER_LEN]

    if not in_scope:
        return {
            "in_scope": False,
            "rewritten_question": "",
            "answer": answer or (
                "I'm specialized in OpenMediaVault, NAS management, and Linux storage."
            ),
        }

    if not rewritten:
        rewritten = question

    return {
        "in_scope": True,
        "rewritten_question": rewritten,
        "answer": answer,
    }


def answer_question(question: str, context_page: str = "") -> str | None:
    """Ask local Ollama for a bounded OMV/NAS/Linux answer when KB misses."""
    question = str(question or "").strip()[:MAX_PROMPT_QUESTION_LEN]
    context_page = str(context_page or "").strip()[:200]
    if not question:
        return None

    prompt = f"""You are a local-only assistant embedded in OpenMediaVault.
No internet access is available. Do not claim to browse, search, download, or
fetch remote information. Do not execute commands. You may explain what the user
can check in the OMV UI or shell, but make clear the user must run commands.

Answer only if the question is about OpenMediaVault, NAS administration, Linux
storage, filesystems, disks, RAID, bcache, SMART, services, system load, memory,
network, updates, logs, Docker on the NAS, or the OMV Agent itself.

If the question is outside that scope, reply with:
I'm specialized in OpenMediaVault, NAS management, and Linux storage.

Keep the answer concise, practical, and offline.

Context page: {context_page}
User question: {question}
"""
    answer = _ollama_generate(prompt)
    if not answer:
        return None
    return answer[:MAX_ANSWER_LEN]

