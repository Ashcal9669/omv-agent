# openmediavault-agent — Moderator Review Request

**Package:** `openmediavault-agent` v1.6.9
**GitHub:** https://github.com/Ashcal9669/omv-agent
**Requires:** OMV 8, Debian Bookworm/Trixie, ARM64 + AMD64

---

## What It Does

A floating assistant widget embedded in every OMV 8 page. Answers NAS, storage, and Linux questions using **live system data** (temperatures, RAID state, bcache, service health, disk usage) while staying fully local. A red badge alerts the user when something goes wrong. System change suggestions always require explicit user confirmation before the command is shown. **The assistant cannot execute anything.**

---

## Security at a Glance

| Concern | Answer |
|---|---|
| Runs as root? | Probe daemon only — required for smartctl/hwmon. No socket, unreachable from network. |
| Network exposure? | Flask bound to `127.0.0.1:11111` only. nginx proxies with rate limiting (10 req/min). |
| Outbound calls? | Zero by default. Optional Ollama fallback is loopback-only and only for in-scope OMV/NAS/Linux phrasing. |
| Watcher daemon? | `PrivateNetwork=yes`, `CapabilityBoundingSet=` (empty) — kernel-enforced isolation. |
| UI injection safe? | All responses use `textContent`, never `innerHTML`. No XSS path. |
| CSRF? | `Origin` header validated on every POST. |
| Modifies OMV core? | No. Additive only — uses OMV extension points and nginx drop-ins. |
| Clean uninstall? | `apt remove` stops daemons, removes users, cleans nginx config. |

---

## Three-Daemon Design

```
Browser → nginx → omv-agent.service     (www-data, reads SQLite, returns text)
                       ↑
                  SQLite DB
                       ↑
          omv-agent-probe.service        (root, hardware telemetry, no socket)
          omv-agent-watch.service        (dedicated user, PrivateNetwork=yes)

Optional: omv-agent.service → local Ollama on 127.0.0.1:11434 only
```

A compromise of the Flask API gains `www-data` only — cannot touch the probe or watcher pipelines.

---

## OMV Plugin Compliance

- Package: `openmediavault-agent` — correct naming convention
- `Depends: openmediavault (>= 8)` — version-pinned
- `Section: admin`, `Architecture: all` — correct
- Uses `navigation.d/`, `route.d/`, `component.d/`, `openmediavault-webgui.d/` — designated extension points only
- `postinst`/`prerm`/`postrm` — full dpkg lifecycle handled

---

## Tested On

- Raspberry Pi 5, ARM64, OMV 8, Debian Bookworm ✅
- Raspberry Pi 5, ARM64, OMV 8, Debian Trixie ✅
- AMD64 — architecture declared, community testing needed

---

## Known Limitations

- apt repo not yet GPG-signed (`[trusted=yes]` required) — signing is still pending
- Widget is injected via nginx `sub_filter` so the assistant is present on every page; deeper native workbench integration would still be cleaner

---

## Install

```bash
echo "deb [trusted=yes] https://Ashcal9669.github.io/omv-agent ./" | sudo tee /etc/apt/sources.list.d/omv-agent.list
sudo apt update && sudo apt install openmediavault-agent
sudo nginx -t && sudo systemctl reload nginx
```

---

## Ask to Moderators

**@ryecoaaron** — does this meet the bar for omv-extras? What would need to change? I will make those changes.

**To all moderators** — is anything in the security model a concern? I want to address it before wider community exposure.

Source is fully public. Nothing compiled or obfuscated. Happy to answer any questions.

*v1.6.8 — May 2026*
