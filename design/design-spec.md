# OMV Agent Widget — Design Specification

Version: 1.0.0
Date: 2026-02-18
Author: Agent 1 (Designer), VIBE STUDIO

---

## 1. Color Palette

### Primary Brand

| Token               | Value                      | Usage                                       |
|---------------------|----------------------------|---------------------------------------------|
| `--omv-blue`        | `#0d7ab3`                  | FAB, send button, user bubbles, focus rings |
| `--omv-blue-hover`  | `#0e8fcf`                  | Hover states on blue elements               |
| `--omv-blue-dim`    | `rgba(13,122,179,0.18)`    | Focus glow, subtle highlights               |
| `--omv-blue-ring`   | `rgba(13,122,179,0.45)`    | FAB pulse ring                              |

### Surfaces (dark theme)

| Token                   | Value       | Usage                                  |
|-------------------------|-------------|----------------------------------------|
| `--omv-panel-bg`        | `#1e2228`   | Panel background                       |
| `--omv-panel-border`    | `#333740`   | All borders and dividers               |
| `--omv-panel-header-bg` | `rgba(30,34,40,0.82)` | Glass-morphism header        |
| `--omv-surface-1`       | `#252930`   | Input area, code blocks, context bar   |
| `--omv-surface-2`       | `#2d3139`   | Agent bubbles, textarea                |
| `--omv-surface-3`       | `#363b45`   | Hover states on surface elements       |

### Text

| Token                 | Value     | Usage                                 |
|-----------------------|-----------|---------------------------------------|
| `--omv-text-primary`  | `#e8eaed` | Body text, message content            |
| `--omv-text-secondary`| `#9aa0ab` | Subtitles, secondary labels           |
| `--omv-text-muted`    | `#636a76` | Timestamps, hints, placeholders       |

### Semantic

| Token                  | Value     | Usage                                  |
|------------------------|-----------|----------------------------------------|
| `--omv-warning`        | `#f0a500` | Warning dialog, confirm button         |
| `--omv-danger`         | `#d9534f` | Destructive action buttons             |
| `--omv-success`        | `#3bb273` | Status dot "ready" color               |

### Section Context Colors (context bar)

| Section    | Color     |
|------------|-----------|
| storage    | `#59a0d8` |
| network    | `#6bb56b` |
| services   | `#c87adc` |
| system     | `#e5833a` |
| users      | `#d4c84e` |
| dashboard  | `#5ab8c4` |

---

## 2. Animation Timings

| Name                 | Duration | Easing                          | Usage                               |
|----------------------|----------|---------------------------------|-------------------------------------|
| `--omv-transition-panel`   | 350ms | `cubic-bezier(0.4,0,0.2,1)` | Panel slide-in/out              |
| `--omv-transition-medium`  | 280ms | `ease`                       | Overlay fade, dialog pop-in     |
| `--omv-transition-fast`    | 180ms | `ease`                       | Hover/focus states, buttons     |
| `omv-pulse-ring`     | 2400ms   | infinite                        | FAB idle pulse (stops when open)    |
| `omv-status-pulse`   | 3000ms   | infinite                        | Header status dot breathing         |
| `omv-typing-bounce`  | 1350ms   | infinite, staggered 180ms each  | 3-dot typing indicator              |
| `omv-bubble-in`      | 220ms    | spring cubic-bezier             | New message appearance              |
| `omv-dialog-in`      | 280ms    | spring cubic-bezier             | Warning dialog entrance             |

All animations disabled under `@media (prefers-reduced-motion: reduce)`.

---

## 3. Component Hierarchy

```
#omv-agent-root
│
├── .omv-agent-overlay              (z: 99997) backdrop
├── .omv-agent-fab                  (z: 99999) floating action button
│   └── .omv-agent-fab__icon
│
├── .omv-agent-panel                (z: 99998) aside[role=dialog]
│   ├── .omv-agent-panel__header
│   │   ├── .omv-agent-panel__logo-mark
│   │   ├── .omv-agent-panel__title-group
│   │   │   ├── .omv-agent-panel__title
│   │   │   └── .omv-agent-panel__subtitle
│   │   ├── .omv-agent-panel__status-dot (data-status: ready|thinking|offline)
│   │   └── .omv-agent-panel__close
│   │
│   ├── .omv-agent-context-bar      (data-section: storage|network|services|system|users|dashboard)
│   │   ├── .omv-agent-context-bar__icon
│   │   ├── .omv-agent-context-bar__dot
│   │   ├── .omv-agent-context-bar__crumb
│   │   └── .omv-agent-context-bar__section
│   │
│   ├── .omv-agent-messages         (role=log, aria-live=polite)
│   │   ├── .omv-agent-messages__separator
│   │   └── .omv-agent-message (--user | --agent)
│   │       ├── .omv-agent-message__bubble
│   │       │   └── [.omv-agent-message__actions] (suggestions only)
│   │       │       └── .omv-agent-message__action-btn (--danger variant)
│   │       └── .omv-agent-message__meta > time
│   │
│   ├── .omv-agent-typing           (.is-visible when thinking)
│   │   └── .omv-agent-typing__dot × 3
│   │
│   └── .omv-agent-input-area
│       ├── .omv-agent-input-area__row
│       │   ├── .omv-agent-textarea
│       │   └── .omv-agent-send-btn
│       └── .omv-agent-input-area__hint
│
└── .omv-agent-warning-overlay      (z: 100000) role=alertdialog
    └── .omv-agent-warning-dialog
        ├── .omv-agent-warning-dialog__header
        ├── .omv-agent-warning-dialog__body
        │   ├── .omv-agent-warning-dialog__description (JS-populated)
        │   ├── .omv-agent-warning-dialog__command      (JS-populated)
        │   └── .omv-agent-warning-dialog__impact       (JS-populated)
        └── .omv-agent-warning-dialog__footer
            ├── .omv-agent-warning-btn--cancel
            └── .omv-agent-warning-btn--confirm
```

---

## 4. Warning Dialog — Suggestion Flow

1. Agent generates suggestion with "Apply" action button
2. User clicks "Apply" — JS intercepts, reads `data-action-id`
3. JS populates: description, command block, impact note in dialog
4. Dialog shown: `is-visible` added, `aria-hidden` removed, focus → Cancel
5. User **confirms** → `omv-agent:action-confirmed` event fired → dialog closes
6. User **cancels** → dialog closes, no action, focus returns to button

**Security:** The helper NEVER executes anything. The "Apply" button only shows the suggestion to the user — the user must run any commands themselves manually.

**Keyboard:**
- `Escape` = cancel dialog
- `Tab` cycles between Cancel and Confirm (focus trap)
- After close, focus returns to triggering element

---

## 5. Z-Index Stacking

| Layer           | z-index |
|-----------------|---------|
| Warning overlay | 100000  |
| FAB             | 99999   |
| Panel           | 99998   |
| Backdrop        | 99997   |
| OMV Angular app | < 1000  |

---

## 6. Responsive

| Breakpoint  | Panel width | FAB           | Warning dialog           |
|-------------|-------------|---------------|--------------------------|
| >= 480px    | 380px fixed | bottom/right 28px | Centered modal 360px |
| < 480px     | 100vw       | bottom/right 20px | Bottom sheet             |
