# OMV Agent Helper

An intelligent, context-aware assistant for **OpenMediaVault 8** — a persistent floating widget that answers NAS, Linux, and storage questions using live system data.

## Features

- **Animated FAB widget** injected into every OMV page via nginx `sub_filter`
- **Live system probes** — drive temps, RAID status, bcache state, load, disk usage, network, services
- **Dynamic service discovery** — no hardcoded lists; finds every service, process, and device on the system automatically
- **Proactive alerts** — dedicated watcher daemon monitors journald, service states, and thresholds in real time
- **Smart intent detection** — routes queries to live data or knowledge base as appropriate
- **Off-topic rejection** — only answers OMV / NAS / Linux questions
- **System change gate** — any suggestion involving a system change requires explicit confirmation before being shown
- **Fully offline** — no external API calls, no cloud dependency

## Architecture

```
[root]            omv-agent-probe.service   →  /run/omv-agent/probe_cache.json   (5s / 30s)
[omv-agent-watch] omv-agent-watch.service   →  /run/omv-agent/event_queue.json   (continuous)
[www-data]        omv-agent.service (Flask)  →  127.0.0.1:11111
[nginx]           sub_filter + proxy         →  /omv-agent/
[browser]         widget.js                  →  FAB button, chat panel, alert badge
```

## Services

| Service | User | Role |
|---|---|---|
| `omv-agent-probe` | root | Collects live system data every 5s/30s |
| `omv-agent-watch` | omv-agent-watch | Observes journal, services, devices — emits alerts |
| `omv-agent` | www-data | Flask API — answers queries, serves events |

## Install

```bash
sudo dpkg -i dist/openmediavault-agent_1.2.0_all.deb
sudo nginx -t && sudo systemctl reload nginx
```

**Requirements:** OpenMediaVault 8, Python 3.9+, python3-flask, nginx

## Build

```bash
bash build.sh
```

## Security Model

- The agent **cannot execute any commands** — it only provides guidance
- All system change suggestions are **gated behind a confirmation dialog**
- The watcher daemon runs with **zero capabilities**, `PrivateNetwork=yes`, `ProtectSystem=strict`
- Flask backend rejects all requests not originating from nginx on loopback (`127.0.0.1`)
- All user input is sanitized; responses rendered via `textContent` only (no `innerHTML`)

## Version

`1.2.0` — Watcher daemon + dynamic discovery + bcache/updates/service probes + 5s anomaly scanning
