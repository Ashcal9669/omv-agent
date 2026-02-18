# OMV Agent Security Fixes

**Date:** 2026-02-18
**Author:** Agent 3 - Security Penetrator
**Status:** PRE-IMPLEMENTATION. These are required code patterns for Agent 2 to implement from the start. Not patches -- these are the correct implementations.

---

## FIX-CRIT-1: Unauthenticated LAN Access

### File: /etc/nginx/openmediavault-webgui.d/omv-agent.conf (NEW FILE)

```nginx
# OMV Agent nginx location block
# Place in /etc/nginx/openmediavault-webgui.d/omv-agent.conf
limit_req_zone $binary_remote_addr zone=omv_agent:10m rate=10r/m;

server {
    # This block extends the existing OMV server config.
    # Add these location blocks inside the existing server {} context.

    location /omv-agent/ {
        # MANDATORY: OMV session auth enforcement
        auth_request /rpc.php;
        auth_request_set $auth_status $upstream_status;

        # Rate limiting
        limit_req zone=omv_agent burst=5 nodelay;
        limit_req_status 429;

        # Input size cap
        client_max_body_size 8k;

        # Proxy to Flask on loopback only
        proxy_pass http://127.0.0.1:5000/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_connect_timeout 5s;
    }
}
```

### File: app.py -- Flask binding and loopback enforcement

```python
from flask import Flask, request, abort
import os

app = Flask(__name__)
app.config["DEBUG"] = False
app.config["TESTING"] = False
app.config["PROPAGATE_EXCEPTIONS"] = False

@app.before_request
def require_local_proxy():
    """Reject any request not arriving from nginx on loopback."""
    if request.remote_addr != "127.0.0.1":
        abort(403)

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",  # NEVER 0.0.0.0
        port=5000,
        debug=False         # NEVER True in production
    )
```

---

## FIX-CRIT-2: XSS in widget.js

### File: widget.js

```javascript
// CORRECT implementation of response rendering:
// NEVER use innerHTML, outerHTML, document.write, insertAdjacentHTML

function renderResponse(container, responseText) {
    // Clear previous content safely
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }
    // Render AI response as plain text ONLY
    container.textContent = responseText;
}

// Correct fetch and render pipeline:
async function submitQuestion(question) {
    const responseContainer = document.getElementById("omv-agent-response");
    responseContainer.textContent = "Thinking...";

    try {
        const resp = await fetch("/omv-agent/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: question })
        });
        if (\!resp.ok) {
            responseContainer.textContent = "Error: " + resp.status;
            return;
        }
        const data = await resp.json();
        // textContent only -- never innerHTML
        responseContainer.textContent = data.answer || "No response.";
    } catch (err) {
        responseContainer.textContent = "Connection error.";
    }
}
```

If markdown formatting is required later, wrap with:
```javascript
// ONLY acceptable innerHTML usage -- requires both libraries present
const raw = marked.parse(data.answer);          // marked.js
const safe = DOMPurify.sanitize(raw);           // DOMPurify
responseContainer.innerHTML = safe;             // only after sanitization
```

---

## FIX-CRIT-3: Command Injection Prevention

File: brain.py

ALLOWED_COMMANDS dict maps string keys to pre-approved command lists.
run_system_check(command_key) validates key is in ALLOWED_COMMANDS, then calls subprocess.run() with shell=False and the list form.
User text goes to AI classifier only. Classifier outputs a key string. Key is validated. User text never reaches subprocess.

NEVER use: os.system(), os.popen(), subprocess.run(shell=True), eval(), exec() with any user-derived data.

---

## FIX-CRIT-4: SQL Injection Prevention

File: brain.py

All SQLite queries MUST use ? placeholder syntax:
  cursor.execute("SELECT * FROM history WHERE q = ?", (user_input,))
  cursor.execute("SELECT * FROM kb WHERE topic = ?", (topic,))
  cursor.execute("INSERT INTO log (q, a, ts) VALUES (?, ?, ?)", (q, a, ts))

On every connection open:
  conn.execute("PRAGMA trusted_schema = OFF")
  conn.execute("PRAGMA secure_delete = ON")
  conn.execute("PRAGMA journal_mode = WAL")
  # Never call conn.enable_load_extension(True)

NEVER use f-strings, % formatting, or string concatenation in SQL.

---

## FIX-MED-1: CSRF Prevention

File: app.py

Add to app.py:

  @app.before_request
  def enforce_json_content_type():
      if request.method in ("POST", "PUT", "PATCH"):
          if not request.is_json:
              abort(415)

  @app.route("/query", methods=["POST"])   # POST only, NEVER GET
  def query():
      origin = request.headers.get("Origin", "")
      host = request.headers.get("Host", "").split(":")[0]
      if origin and origin not in (f"http://{host}", f"https://{host}"):
          abort(403)

---

## FIX-MED-2: Flask Error Handlers

File: app.py

  app.config["DEBUG"] = False
  app.config["PROPAGATE_EXCEPTIONS"] = False

  @app.errorhandler(Exception)
  def handle_all_errors(e):
      app.logger.error("Unhandled exception", exc_info=True)
      return {"error": "An internal error occurred."}, 500

  @app.errorhandler(400)
  def bad_request(e): return {"error": "Bad request."}, 400

  @app.errorhandler(403)
  def forbidden(e): return {"error": "Forbidden."}, 403

  @app.errorhandler(413)
  def too_large(e): return {"error": "Request too large."}, 413

  @app.errorhandler(415)
  def unsupported_media(e): return {"error": "Content-Type must be application/json."}, 415

  @app.errorhandler(429)
  def rate_limited(e): return {"error": "Too many requests."}, 429

---

## FIX-MED-3: Input Length Enforcement

File: app.py

  MAX_QUESTION_BYTES = 2048

  @app.route("/query", methods=["POST"])
  def query():
      data = request.get_json(force=False, silent=True)
      if data is None:
          abort(400)
      question = data.get("question", "")
      if not isinstance(question, str):
          abort(400)
      if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
          return {"error": "Question exceeds 2048 byte limit."}, 413

File: nginx conf -- add inside /omv-agent/ location block:
  client_max_body_size 8k;

---

## FIX-MED-4: Rate Limiting

File: /etc/nginx/openmediavault-webgui.d/omv-agent.conf

Add at server/http level:
  limit_req_zone $binary_remote_addr zone=omv_agent:10m rate=10r/m;

Inside /omv-agent/query location block:
  limit_req zone=omv_agent burst=5 nodelay;
  limit_req_status 429;

---

## FIX-MED-5: History Table Pruning

File: brain.py

  MAX_HISTORY_ENTRIES = 500

  def insert_history(conn, question, answer, timestamp):
      conn.execute(
          "INSERT INTO history (q, a, created_at) VALUES (?, ?, ?)",
          (question, answer, timestamp)
      )
      conn.execute(
          "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY created_at DESC LIMIT ?)",
          (MAX_HISTORY_ENTRIES,)
      )
      conn.commit()

---

## FIX-MED-6: Systemd Service Unit

File: /lib/systemd/system/omv-agent.service (or package equivalent path)

[Unit]
Description=OMV Agent AI Backend
After=network.target
Requires=network.target

[Service]
Type=simple
User=www-data
Group=www-data
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=yes
ReadWritePaths=/var/lib/omv-agent /var/log/omv-agent
CapabilityBoundingSet=
AmbientCapabilities=
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LimitNOFILE=1024
LimitNPROC=64
ExecStart=/usr/bin/python3 /usr/lib/omv-agent/app.py
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/omv-agent/app.log
StandardError=append:/var/log/omv-agent/app.log

[Install]
WantedBy=multi-user.target

---

## END OF SECURITY FIXES DOCUMENT

All fixes above are REQUIRED implementations, not optional patches.
Agent 2 must implement all CRITICAL and MEDIUM fixes before any code ships.
Agent 3 will re-audit once code is written.
