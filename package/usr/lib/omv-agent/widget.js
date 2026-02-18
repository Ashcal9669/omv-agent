/**
 * OMV Agent Helper Widget
 * Injected into every OMV 8 page via nginx sub_filter.
 * Vanilla JS, no external dependencies. IIFE-scoped to avoid Angular conflicts.
 */
(function () {
  'use strict';

  // ── Config ──────────────────────────────────────────────────────────────────
  var API_BASE = '/omv-agent';
  var MAX_Q_LEN = 500;
  var SESSION_KEY = 'omv_agent_session';
  var HISTORY_KEY = 'omv_agent_history';

  // ── Session ID ───────────────────────────────────────────────────────────────
  function getSessionId() {
    var sid = sessionStorage.getItem(SESSION_KEY);
    if (!sid) {
      sid = 'omv-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 9);
      sessionStorage.setItem(SESSION_KEY, sid);
    }
    return sid;
  }

  // ── Context Detection ────────────────────────────────────────────────────────
  var PAGE_LABELS = {
    '/dashboard':          { label: 'Dashboard',               icon: '📊' },
    '/storage':            { label: 'Storage',                 icon: '💾' },
    '/storage/disks':      { label: 'Storage › Disks',         icon: '💽' },
    '/storage/filesystems':{ label: 'Storage › Filesystems',   icon: '📁' },
    '/storage/shared-folders':{ label: 'Storage › Shares',     icon: '📂' },
    '/storage/mdadm':      { label: 'Storage › RAID',          icon: '🔗' },
    '/network':            { label: 'Network',                 icon: '🌐' },
    '/network/interfaces': { label: 'Network › Interfaces',    icon: '🔌' },
    '/services':           { label: 'Services',                icon: '⚙️'  },
    '/services/smb':       { label: 'Services › SMB/CIFS',     icon: '🖥️'  },
    '/services/nfs':       { label: 'Services › NFS',          icon: '📡' },
    '/services/ssh':       { label: 'Services › SSH',          icon: '🔐' },
    '/system':             { label: 'System',                  icon: '🔧' },
    '/system/users':       { label: 'System › Users',          icon: '👤' },
    '/system/plugins':     { label: 'System › Plugins',        icon: '🔌' },
    '/system/updates':     { label: 'System › Updates',        icon: '🔄' },
    '/system/certificates':{ label: 'System › Certificates',   icon: '🔒' },
    '/diagnostics':        { label: 'Diagnostics',             icon: '🩺' },
  };

  function getCurrentContext() {
    var path = window.location.pathname || '/';
    // Strip trailing slash
    path = path.replace(/\/$/, '') || '/';
    // Try longest matching prefix
    var best = null, bestLen = 0;
    for (var k in PAGE_LABELS) {
      if (path.startsWith(k) && k.length > bestLen) {
        best = PAGE_LABELS[k];
        bestLen = k.length;
      }
    }
    return best || { label: 'OpenMediaVault', icon: '🖧' };
  }

  function getCurrentPage() {
    return window.location.pathname || '/';
  }

  // ── CSS ──────────────────────────────────────────────────────────────────────
  var CSS = `
    #omv-agent-root * { box-sizing: border-box; font-family: 'Inter', system-ui, sans-serif; }

    /* FAB Button */
    #omv-agent-fab {
      position: fixed; bottom: 28px; right: 28px; z-index: 99999;
      width: 56px; height: 56px; border-radius: 50%;
      background: #0d7ab3; color: #fff; border: none; cursor: pointer;
      box-shadow: 0 4px 16px rgba(13,122,179,0.5);
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
      outline: none;
    }
    #omv-agent-fab:hover { transform: scale(1.1); box-shadow: 0 6px 24px rgba(13,122,179,0.7); }
    #omv-agent-fab:focus-visible { outline: 3px solid #fff; outline-offset: 2px; }
    /* Notification badge on FAB */
    #omv-agent-badge {
      position: absolute; top: -3px; right: -3px;
      min-width: 18px; height: 18px; padding: 0 4px;
      background: #e53935; color: #fff;
      border-radius: 9px; font-size: 10px; font-weight: 700;
      display: none; align-items: center; justify-content: center;
      border: 2px solid #1e2228; pointer-events: none;
      line-height: 1;
    }
    #omv-agent-badge.visible { display: flex; }
    #omv-agent-fab svg { width: 26px; height: 26px; pointer-events: none; }

    /* Pulse ring */
    #omv-agent-fab::before {
      content: ''; position: absolute; width: 56px; height: 56px;
      border-radius: 50%; border: 2px solid #0d7ab3;
      animation: omv-pulse 2.5s ease-out infinite;
    }
    @keyframes omv-pulse {
      0%   { transform: scale(1);   opacity: 0.8; }
      70%  { transform: scale(1.6); opacity: 0;   }
      100% { transform: scale(1.6); opacity: 0;   }
    }
    @media (prefers-reduced-motion: reduce) {
      #omv-agent-fab::before { animation: none; }
    }

    /* Overlay */
    #omv-agent-overlay {
      display: none; position: fixed; inset: 0; z-index: 99997;
      background: rgba(0,0,0,0.45); backdrop-filter: blur(2px);
      animation: omv-fade-in 0.2s ease;
    }
    #omv-agent-overlay.open { display: block; }
    @keyframes omv-fade-in { from { opacity: 0; } to { opacity: 1; } }

    /* Panel */
    #omv-agent-panel {
      position: fixed; top: 0; right: 0; z-index: 99998;
      width: 380px; height: 100vh;
      background: #1e2228; border-left: 1px solid #333;
      display: flex; flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: -4px 0 32px rgba(0,0,0,0.4);
    }
    #omv-agent-panel.open { transform: translateX(0); }
    @media (max-width: 480px) {
      #omv-agent-panel { width: 100vw; }
    }

    /* Panel Header */
    #omv-agent-header {
      padding: 16px 18px 12px;
      background: linear-gradient(135deg, #0d7ab3 0%, #0a5c87 100%);
      display: flex; align-items: center; gap: 10px;
      flex-shrink: 0;
    }
    #omv-agent-header-icon { font-size: 22px; }
    #omv-agent-header-title {
      flex: 1; color: #fff; font-size: 15px; font-weight: 600; line-height: 1.2;
    }
    #omv-agent-header-sub { color: rgba(255,255,255,0.75); font-size: 11px; }
    #omv-agent-close {
      background: none; border: none; cursor: pointer;
      color: rgba(255,255,255,0.8); font-size: 20px; line-height: 1;
      padding: 4px 6px; border-radius: 4px; transition: background 0.15s;
    }
    #omv-agent-close:hover { background: rgba(255,255,255,0.15); color: #fff; }

    /* Context Bar */
    #omv-agent-context {
      padding: 7px 18px; background: #252930; border-bottom: 1px solid #333;
      font-size: 12px; color: #aaa; display: flex; align-items: center; gap: 6px;
      flex-shrink: 0;
    }
    #omv-agent-context-icon { font-size: 14px; }

    /* Messages */
    #omv-agent-messages {
      flex: 1; overflow-y: auto; padding: 16px 14px;
      display: flex; flex-direction: column; gap: 10px;
      scroll-behavior: smooth;
    }
    #omv-agent-messages::-webkit-scrollbar { width: 4px; }
    #omv-agent-messages::-webkit-scrollbar-track { background: transparent; }
    #omv-agent-messages::-webkit-scrollbar-thumb { background: #444; border-radius: 2px; }

    /* Message bubbles */
    .omv-msg { max-width: 88%; padding: 10px 14px; border-radius: 12px; font-size: 13.5px; line-height: 1.5; word-break: break-word; }
    .omv-msg-user { align-self: flex-end; background: #0d7ab3; color: #fff; border-bottom-right-radius: 3px; }
    .omv-msg-agent { align-self: flex-start; background: #2d3139; color: #e0e0e0; border-bottom-left-radius: 3px; }
    .omv-msg-system { align-self: center; background: #1a1d22; color: #888; font-size: 12px; font-style: italic; border-radius: 6px; padding: 6px 12px; }
    .omv-msg-agent strong { color: #5bb8f5; }
    .omv-msg-agent code { background: #1a1d22; padding: 1px 5px; border-radius: 3px; font-family: 'Roboto Mono', monospace; font-size: 12px; color: #a8d8a8; }

    /* Typing indicator */
    #omv-agent-typing { align-self: flex-start; display: none; gap: 5px; padding: 12px 16px; background: #2d3139; border-radius: 12px; border-bottom-left-radius: 3px; }
    #omv-agent-typing.visible { display: flex; }
    #omv-agent-typing span { width: 7px; height: 7px; background: #888; border-radius: 50%; animation: omv-bounce 1.2s ease-in-out infinite; }
    #omv-agent-typing span:nth-child(2) { animation-delay: 0.2s; }
    #omv-agent-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes omv-bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-6px)} }
    @media (prefers-reduced-motion: reduce) {
      #omv-agent-typing span { animation: none; background: #aaa; }
    }

    /* Input area */
    #omv-agent-input-area {
      padding: 12px 14px 16px; background: #252930; border-top: 1px solid #333;
      display: flex; gap: 8px; align-items: flex-end; flex-shrink: 0;
    }
    #omv-agent-input {
      flex: 1; background: #1e2228; border: 1px solid #3a3f4a; border-radius: 10px;
      color: #e0e0e0; font-size: 13.5px; line-height: 1.4; padding: 9px 12px;
      resize: none; min-height: 40px; max-height: 120px; overflow-y: auto;
      outline: none; transition: border-color 0.2s;
      font-family: inherit;
    }
    #omv-agent-input:focus { border-color: #0d7ab3; }
    #omv-agent-input::placeholder { color: #555; }
    #omv-agent-send {
      width: 40px; height: 40px; background: #0d7ab3; border: none; border-radius: 10px;
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      transition: background 0.15s, transform 0.1s; flex-shrink: 0;
    }
    #omv-agent-send:hover { background: #0a5c87; }
    #omv-agent-send:active { transform: scale(0.95); }
    #omv-agent-send svg { width: 18px; height: 18px; fill: #fff; }
    #omv-agent-send:disabled { background: #333; cursor: not-allowed; }

    /* Warning dialog */
    #omv-agent-warning {
      display: none; position: fixed; inset: 0; z-index: 100000;
      align-items: center; justify-content: center;
      background: rgba(0,0,0,0.6);
      animation: omv-fade-in 0.2s ease;
    }
    #omv-agent-warning.show { display: flex; }
    #omv-agent-warning-box {
      background: #1e2228; border: 2px solid #f0a500; border-radius: 14px;
      padding: 24px 28px; max-width: 380px; width: 90%; text-align: center;
      box-shadow: 0 8px 40px rgba(0,0,0,0.6);
      animation: omv-slide-up 0.25s ease;
    }
    @keyframes omv-slide-up { from { transform: translateY(24px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
    #omv-agent-warning-icon { font-size: 36px; margin-bottom: 10px; }
    #omv-agent-warning-title { color: #f0a500; font-size: 16px; font-weight: 700; margin-bottom: 8px; }
    #omv-agent-warning-msg { color: #ccc; font-size: 13px; line-height: 1.5; margin-bottom: 20px; }
    #omv-agent-warning-actions { display: flex; gap: 12px; justify-content: center; }
    .omv-warn-btn {
      padding: 9px 22px; border-radius: 8px; font-size: 13px; font-weight: 600;
      cursor: pointer; border: none; transition: opacity 0.15s;
    }
    .omv-warn-btn:hover { opacity: 0.85; }
    #omv-warn-cancel { background: #333; color: #ccc; }
    #omv-warn-confirm { background: #f0a500; color: #111; }
  `;

  // ── HTML ─────────────────────────────────────────────────────────────────────
  var HTML = `
    <div id="omv-agent-root" role="complementary" aria-label="OMV Agent Helper">
      <!-- FAB -->
      <button id="omv-agent-fab" aria-label="Open OMV Agent Helper" aria-expanded="false" aria-controls="omv-agent-panel">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="11" width="18" height="10" rx="2"/>
          <circle cx="9" cy="16" r="1" fill="currentColor"/>
          <circle cx="15" cy="16" r="1" fill="currentColor"/>
          <path d="M9 7V5a3 3 0 0 1 6 0v2"/>
          <line x1="12" y1="3" x2="12" y2="5"/>
        </svg>
        <span id="omv-agent-badge" aria-label="New alerts"></span>
      </button>

      <!-- Overlay -->
      <div id="omv-agent-overlay" aria-hidden="true"></div>

      <!-- Panel -->
      <aside id="omv-agent-panel" role="dialog" aria-modal="false" aria-label="OMV Agent Helper Panel">
        <div id="omv-agent-header">
          <span id="omv-agent-header-icon" aria-hidden="true">🤖</span>
          <div id="omv-agent-header-title">
            OMV Agent
            <div id="omv-agent-header-sub">NAS &amp; Linux Assistant</div>
          </div>
          <button id="omv-agent-close" aria-label="Close Agent Helper">✕</button>
        </div>

        <div id="omv-agent-context" aria-live="polite">
          <span id="omv-agent-context-icon" aria-hidden="true">📊</span>
          <span id="omv-agent-context-label">OpenMediaVault</span>
        </div>

        <div id="omv-agent-messages" role="log" aria-live="polite" aria-relevant="additions">
          <div class="omv-msg omv-msg-agent" data-hash="welcome">
            Hello! I'm your OMV Agent. I can help with NAS management, Linux filesystems, disk operations, network shares, and OMV settings.<br><br>
            <strong>Tip:</strong> I only answer OMV/NAS/Linux questions. Ask me anything!
          </div>
        </div>

        <div id="omv-agent-typing" role="status" aria-label="Agent is typing">
          <span></span><span></span><span></span>
        </div>

        <div id="omv-agent-input-area">
          <textarea
            id="omv-agent-input"
            placeholder="Ask about disks, shares, filesystems…"
            rows="1"
            maxlength="500"
            aria-label="Ask the OMV Agent"
          ></textarea>
          <button id="omv-agent-send" aria-label="Send message">
            <svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>
          </button>
        </div>
      </aside>

      <!-- Warning dialog -->
      <div id="omv-agent-warning" role="alertdialog" aria-modal="true" aria-labelledby="omv-agent-warning-title" aria-describedby="omv-agent-warning-msg">
        <div id="omv-agent-warning-box">
          <div id="omv-agent-warning-icon" aria-hidden="true">⚠️</div>
          <div id="omv-agent-warning-title">System Change Warning</div>
          <div id="omv-agent-warning-msg"></div>
          <div id="omv-agent-warning-actions">
            <button class="omv-warn-btn" id="omv-warn-cancel">Cancel</button>
            <button class="omv-warn-btn" id="omv-warn-confirm">Show Suggestion</button>
          </div>
        </div>
      </div>
    </div>
  `;

  // ── Init DOM ─────────────────────────────────────────────────────────────────
  function init() {
    // Inject CSS
    var style = document.createElement('style');
    style.id = 'omv-agent-styles';
    style.textContent = CSS;
    document.head.appendChild(style);

    // Inject HTML
    var container = document.createElement('div');
    container.innerHTML = HTML.trim();
    document.body.appendChild(container.firstElementChild);

    // Wire up
    bindEvents();
    updateContext();
    listenForRouteChanges();
    // Poll for watcher events every 20 seconds
    setInterval(pollEvents, 20000);
    // Initial poll after 5s (give probe time to warm up)
    setTimeout(pollEvents, 5000);
  }

  // ── State ────────────────────────────────────────────────────────────────────
  var isOpen = false;
  var isLoading = false;
  var pendingWarning = null; // { answer, warningMsg, sources }
  var lastEventTimestamp = 0;   // tracks last seen event timestamp
  var unseenEventCount = 0;      // drives badge visibility

  // ── Elements ─────────────────────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }

  // ── Panel open/close ─────────────────────────────────────────────────────────
  function openPanel() {
    isOpen = true;
    el('omv-agent-panel').classList.add('open');
    el('omv-agent-overlay').classList.add('open');
    el('omv-agent-fab').setAttribute('aria-expanded', 'true');
    el('omv-agent-input').focus();
    updateContext();
    updateBadge(0);
  }

  function closePanel() {
    isOpen = false;
    el('omv-agent-panel').classList.remove('open');
    el('omv-agent-overlay').classList.remove('open');
    el('omv-agent-fab').setAttribute('aria-expanded', 'false');
    el('omv-agent-fab').focus();
  }

  // ── Context bar ──────────────────────────────────────────────────────────────
  function updateContext() {
    var ctx = getCurrentContext();
    el('omv-agent-context-icon').textContent = ctx.icon;
    el('omv-agent-context-label').textContent = ctx.label;
  }

  function listenForRouteChanges() {
    // Angular uses pushState — watch for popstate + title changes
    window.addEventListener('popstate', updateContext);
    // MutationObserver on document.title
    var titleObserver = new MutationObserver(function () {
      setTimeout(updateContext, 100);
    });
    var titleEl = document.querySelector('title');
    if (titleEl) {
      titleObserver.observe(titleEl, { childList: true, subtree: true, characterData: true });
    }
    // Also poll Angular router changes via URL
    var lastPath = window.location.pathname;
    setInterval(function () {
      if (window.location.pathname !== lastPath) {
        lastPath = window.location.pathname;
        updateContext();
      }
    }, 500);
  }

  // ── Messaging ────────────────────────────────────────────────────────────────
  /**
   * CRIT-2 FIX: All AI response text is rendered via textContent ONLY.
   * innerHTML is NEVER used for AI-supplied or user-supplied data.
   * This is the sole XSS defence (OMV CSP has unsafe-inline, provides zero protection).
   */
  function addMessage(text, type, hash) {
    var div = document.createElement('div');
    div.className = 'omv-msg omv-msg-' + type;
    if (hash) div.dataset.hash = hash;
    // Render text safely using DOM nodes — no innerHTML
    renderSafeText(div, text);
    el('omv-agent-messages').appendChild(div);
    scrollMessages();
    return div;
  }

  function scrollMessages() {
    var msgs = el('omv-agent-messages');
    msgs.scrollTop = msgs.scrollHeight;
  }

  function updateBadge(count) {
    var badge = el('omv-agent-badge');
    unseenEventCount = count;
    if (count > 0) {
      badge.textContent = count > 9 ? '9+' : String(count);
      badge.classList.add('visible');
    } else {
      badge.classList.remove('visible');
    }
  }

  function pollEvents() {
    fetch(API_BASE + '/events?since=' + lastEventTimestamp, {
      method: 'GET',
      credentials: 'same-origin',
    })
      .then(function(res) { return res.ok ? res.json() : null; })
      .then(function(data) {
        if (!data || !data.events || data.events.length === 0) return;
        var newEvents = data.events;
        // Update timestamp watermark
        for (var i = 0; i < newEvents.length; i++) {
          if (newEvents[i].timestamp > lastEventTimestamp) {
            lastEventTimestamp = newEvents[i].timestamp;
          }
        }
        // Show proactive alert messages if panel is open
        if (isOpen) {
          for (var j = 0; j < newEvents.length; j++) {
            var ev = newEvents[j];
            var icon = ev.level === 'critical' ? '❌' : ev.level === 'warning' ? '⚠️' : 'ℹ️';
            addMessage(icon + ' **' + ev.source + '** — ' + ev.msg, 'agent');
          }
          updateBadge(0);
        } else {
          updateBadge(unseenEventCount + newEvents.length);
        }
      })
      .catch(function() { /* silent — probe may not be ready */ });
  }

  /**
   * Safe text renderer — uses textContent and DOM API only.
   * Supports **bold**, `code`, and newlines without innerHTML.
   * NEVER uses innerHTML, eval, document.write, or insertAdjacentHTML.
   */
  function renderSafeText(container, text) {
    // Clear container safely
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }
    // Split on newlines, render line by line
    var lines = String(text).split('\n');
    for (var i = 0; i < lines.length; i++) {
      if (i > 0) {
        container.appendChild(document.createElement('br'));
      }
      renderLine(container, lines[i]);
    }
  }

  /**
   * Render a single line with **bold** and `code` support.
   * Uses only createTextNode and createElement — zero innerHTML.
   */
  function renderLine(container, line) {
    // Pattern: split on **bold** and `code` markers
    var parts = line.split(/(\*\*[^*]+\*\*|`[^`]+`)/);
    for (var j = 0; j < parts.length; j++) {
      var part = parts[j];
      if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
        var strong = document.createElement('strong');
        strong.textContent = part.slice(2, -2);
        container.appendChild(strong);
      } else if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        var code = document.createElement('code');
        code.textContent = part.slice(1, -1);
        code.style.cssText = 'background:#1a1d22;padding:1px 5px;border-radius:3px;font-family:monospace;font-size:12px;color:#a8d8a8';
        container.appendChild(code);
      } else if (part.length > 0) {
        container.appendChild(document.createTextNode(part));
      }
    }
  }

  function showTyping() {
    el('omv-agent-typing').classList.add('visible');
    scrollMessages();
  }

  function hideTyping() {
    el('omv-agent-typing').classList.remove('visible');
  }

  // ── Warning dialog ────────────────────────────────────────────────────────────
  function showWarning(warningMsg, onConfirm) {
    // CRIT-2: textContent only — warningMsg from backend is treated as untrusted
    el('omv-agent-warning-msg').textContent = String(warningMsg);
    el('omv-agent-warning').classList.add('show');
    el('omv-warn-confirm').onclick = function () {
      el('omv-agent-warning').classList.remove('show');
      onConfirm();
    };
    el('omv-warn-cancel').onclick = function () {
      el('omv-agent-warning').classList.remove('show');
      addMessage('Suggestion cancelled. Ask me anything else!', 'system');
    };
    el('omv-warn-confirm').focus();
  }

  // ── Send query ────────────────────────────────────────────────────────────────
  function sendQuery() {
    if (isLoading) return;
    var input = el('omv-agent-input');
    var question = input.value.trim();
    if (!question) return;
    if (question.length > MAX_Q_LEN) {
      question = question.slice(0, MAX_Q_LEN);
    }

    // Show user message
    addMessage(escapeText(question), 'user');
    input.value = '';
    input.style.height = 'auto';

    isLoading = true;
    el('omv-agent-send').disabled = true;
    showTyping();

    var body = JSON.stringify({
      question: question,
      context_page: getCurrentPage(),
      session_id: getSessionId(),
    });

    fetch(API_BASE + '/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
      credentials: 'same-origin',
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        hideTyping();

        if (data.already_answered) {
          // Find previous message and highlight it
          addMessage('I already answered this above ↑ — scroll up to see my previous response.', 'system');
        } else if (data.is_system_change) {
          // Show warning BEFORE displaying the suggestion
          pendingWarning = data;
          showWarning(
            data.warning_message || 'This suggestion involves a system change.',
            function () {
              addMessage(data.answer, 'agent');
            }
          );
        } else {
          addMessage(data.answer, 'agent');
        }
      })
      .catch(function (err) {
        hideTyping();
        addMessage(
          'The Agent is starting up or temporarily unavailable. Please try again in a moment.',
          'system'
        );
      })
      .finally(function () {
        isLoading = false;
        el('omv-agent-send').disabled = false;
        scrollMessages();
      });
  }

  function escapeText(text) {
    // Used for user message display — textContent handles escaping automatically
    // but we keep this for safety in any legacy path
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // ── Event Binding ─────────────────────────────────────────────────────────────
  function bindEvents() {
    el('omv-agent-fab').addEventListener('click', function () {
      isOpen ? closePanel() : openPanel();
    });

    el('omv-agent-close').addEventListener('click', closePanel);
    el('omv-agent-overlay').addEventListener('click', closePanel);

    el('omv-agent-send').addEventListener('click', sendQuery);

    var input = el('omv-agent-input');
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendQuery();
      }
    });

    // Auto-resize textarea
    input.addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    });

    // Escape to close
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (el('omv-agent-warning').classList.contains('show')) {
          el('omv-agent-warning').classList.remove('show');
          addMessage('Suggestion cancelled.', 'system');
        } else if (isOpen) {
          closePanel();
        }
      }
    });
  }

  // ── Boot ──────────────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
