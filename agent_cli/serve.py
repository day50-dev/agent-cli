import asyncio
import errno
import html
import json
import os
import queue
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from agent_cli import AgentCLI, Output


# ---------------------------------------------------------------------------
# WebOutput — captures Output calls and broadcasts them via an event queue
# ---------------------------------------------------------------------------

class WebOutput(Output):
    """Output subclass that also pushes events to a queue for SSE broadcast."""

    def __init__(self, event_q: queue.Queue, level: int = 0, log_path=None):
        super().__init__(level, log_path)
        self._event_q = event_q

    def _emit(self, event_type: str, text: str):
        try:
            self._event_q.put_nowait({"type": event_type, "data": text})
        except queue.Full:
            try:
                self._event_q.put({"type": event_type, "data": text}, timeout=5)
            except queue.Full:
                pass
        self._record(event_type, text)

    def _record(self, event_type: str, text: str):
        global _current_run_events
        if _current_run_events is not None:
            _current_run_events.append({"type": event_type, "data": text})

    def section(self, text: str) -> None:
        super().section(text)
        self._emit("section", text)

    def info(self, text: str) -> None:
        super().info(text)
        self._emit("info", text)

    def output(self, text: str) -> None:
        super().output(text)
        self._emit("output", text)

    def result(self, text: str) -> None:
        super().result(text)
        self._emit("result", text)

    def success(self, text: str) -> None:
        super().success(text)
        self._emit("success", text)

    def warning(self, text: str) -> None:
        super().warning(text)
        self._emit("warning", text)

    def fatal(self, text: str) -> None:
        super().fatal(text)
        self._emit("fatal", text)

    def markdown(self, text: str) -> None:
        super().markdown(text)
        self._emit("markdown", text)

    def headline(self, text: str) -> None:
        super().headline(text)
        self._emit("headline", text)

    def separator(self) -> None:
        super().separator()
        self._emit("separator", "")

    def kv(self, key: str, value: str) -> None:
        super().kv(key, value)
        self._emit("kv", f"{key}: {value}")

    def subsection(self, text: str) -> None:
        super().subsection(text)
        self._emit("subsection", text)

    def command(self, text: str) -> None:
        super().command(text)
        self._emit("command", text)

    def sublist(self, text: str) -> None:
        super().sublist(text)
        self._emit("sublist", text)

    def prompt(self, text: str, end="\n") -> None:
        super().prompt(text, end)
        self._emit("prompt", text)


# ---------------------------------------------------------------------------
# Global state shared between request handler and background task runner
# ---------------------------------------------------------------------------

_web_server: ThreadingHTTPServer = None
_agent: AgentCLI = None
_event_queue: queue.Queue = queue.Queue(maxsize=5000)
_task_running = threading.Event()
_server_ready = threading.Event()
_server_stop = threading.Event()
_history: list[dict] = []
_history_lock = threading.Lock()
_run_id_counter = 0
_current_run_id: int | None = None
_current_run_events: list[dict] = []
_prompt_request = threading.Event()
_prompt_response = None
_history_path: Path | None = None


def _save_history():
    global _history
    if _history_path is None:
        return
    try:
        _history_path.parent.mkdir(parents=True, exist_ok=True)
        with _history_lock:
            _history_path.write_text(json.dumps(_history, ensure_ascii=False))
    except OSError:
        pass


def _load_history():
    global _history
    if _history_path is None or not _history_path.exists():
        return
    try:
        data = json.loads(_history_path.read_text())
        if isinstance(data, list):
            with _history_lock:
                _history = data
    except (OSError, json.JSONDecodeError):
        pass


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def _sse_message(event_type: str, data: str) -> bytes:
    """Format a single SSE message as bytes."""
    escaped = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"event: output\ndata: {escaped}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>maxac serve</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117; color: #c9d1d9; margin: 0; padding: 0;
    display: flex; min-height: 100vh;
  }

  .sidebar {
    width: 260px; flex-shrink: 0; background: #161b22;
    border-right: 1px solid #30363d; display: flex; flex-direction: column;
    overflow: hidden;
  }
  .sidebar-header {
    padding: 16px; border-bottom: 1px solid #30363d;
  }
  .sidebar-header a { text-decoration: none; }
  .sidebar-header a h1 { font-size: 1rem; margin: 0; color: #f0f6fc; }
  .sidebar-header a h1:hover { color: #58a6ff; }
  .sidebar-logo-link { display: flex; align-items: center; gap: 8px; text-decoration: none; }
  .sidebar-logo { height: 28px; display: block; }
  .sidebar-brand { font-size: 1rem; color: #f0f6fc; font-weight: 600; }
  .sidebar-logo-link:hover .sidebar-brand { color: #58a6ff; }
  .sidebar-header .subtitle { color: #8b949e; font-size: 0.75rem; margin: 2px 0 0; }
  .history-list { flex: 1; overflow-y: auto; padding: 8px 0; }
  .history-item {
    padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #21262d;
    transition: background 0.15s;
  }
  .history-item:hover { background: #1c2128; }
  .history-item.active { background: #1f2937; border-left: 3px solid #58a6ff; padding-left: 13px; }
  .history-item .h-task {
    font-size: 0.85rem; color: #c9d1d9; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
  }
  .history-item .h-meta {
    font-size: 0.75rem; color: #8b949e; margin-top: 2px;
  }
  .history-item .h-indicator {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    margin-right: 4px;
  }
  .history-item .h-indicator.done { background: #3fb950; }
  .history-item .h-indicator.running { background: #d29922; }
  .history-empty {
    padding: 24px 16px; color: #484f58; font-size: 0.85rem; text-align: center;
  }

  .main {
    flex: 1; display: flex; flex-direction: column; min-width: 0;
  }
  .main-content { max-width: 800px; margin: 0 auto; padding: 24px 24px; width: 100%; }

  .skills-panel {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 24px;
  }
  .skills-panel h2 { font-size: 1rem; margin: 0 0 12px; color: #f0f6fc; }
  .skill-list { display: flex; flex-wrap: wrap; gap: 8px; }
  .skill-tag {
    background: #1f2937; color: #58a6ff; border: 1px solid #30363d;
    border-radius: 4px; padding: 4px 10px; font-size: 0.8rem;
    cursor: pointer; transition: background 0.15s;
  }
  .skill-tag:hover { background: #2d3748; }
  .no-skills { color: #8b949e; font-size: 0.85rem; font-style: italic; }

  .config-panel {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 24px;
  }
  .config-panel h2 { font-size: 1rem; margin: 0 0 12px; color: #f0f6fc; }
  .config-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .config-row label { width: 60px; color: #8b949e; font-size: 0.85rem; flex-shrink: 0; }
  .config-row input {
    flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 8px 10px; color: #c9d1d9; font-size: 0.85rem; outline: none;
  }
  .config-row input:focus { border-color: #58a6ff; }
  .config-row input::placeholder { color: #484f58; }
  .config-actions { display: flex; gap: 8px; margin-top: 4px; }
  .config-actions button {
    background: #1f2937; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 6px; padding: 6px 14px; font-size: 0.8rem; cursor: pointer;
  }
  .config-actions button:hover { background: #2d3748; }
  .config-actions button.primary { background: #238636; color: #fff; border: none; }
  .config-actions button.primary:hover { background: #2ea043; }
  .config-saved { color: #3fb950; font-size: 0.8rem; margin-left: 8px; display: none; }
  .preset-pills { display: flex; gap: 6px; margin-bottom: 10px; }
  .preset-pill {
    background: #1f2937; color: #8b949e; border: 1px solid #30363d;
    border-radius: 12px; padding: 3px 12px; font-size: 0.75rem; cursor: pointer;
    transition: all 0.15s;
  }
  .preset-pill:hover { background: #2d3748; color: #c9d1d9; border-color: #58a6ff; }

  .task-form {
    display: flex; flex-direction: column; gap: 8px; margin-bottom: 24px;
  }
  .task-form textarea {
    background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 10px 12px; color: #c9d1d9; font-size: 0.95rem; outline: none;
    resize: vertical; font-family: inherit;
  }
  .task-form textarea:focus { border-color: #58a6ff; }
  .task-actions { display: flex; gap: 8px; justify-content: flex-end; }
  .task-actions button {
    background: #238636; color: #fff; border: none; border-radius: 6px;
    padding: 10px 20px; font-size: 0.95rem; cursor: pointer; white-space: nowrap;
    transition: background 0.15s;
  }
  .task-actions button:hover { background: #2ea043; }
  .task-actions button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
  .task-actions button.muted { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
  .task-actions button.muted:hover { background: #30363d; color: #c9d1d9; }

  #output {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; min-height: 200px; max-height: 600px;
    overflow-y: auto; font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 0.85rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
  }
  #output:empty::before { content: "Output will appear here..."; color: #484f58; }
  #output.waiting::before { content: ""; }
  .spinner-container { display: flex; align-items: center; gap: 10px; padding: 24px 0; color: #8b949e; font-size: 0.85rem; }
  .spinner { width: 18px; height: 18px; border: 2px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #output .s-separator { border: none; border-top: 1px solid #30363d; margin: 12px 0; }
  #output .s-section { color: #f0f6fc; font-weight: 600; font-size: 0.95rem; margin: 12px 0 4px; }
  #output .s-subsection { color: #8b949e; font-weight: 600; font-size: 0.85rem; margin: 8px 0 4px; }
  #output .s-info { color: #8b949e; margin: 2px 0; }
  #output .s-output { color: #c9d1d9; margin: 1px 0; padding-left: 12px; border-left: 2px solid #30363d; }
  #output .s-command { color: #d2a8ff; margin: 2px 0; }
  #output .s-success { color: #3fb950; }
  #output .s-warning { color: #d29922; }
  #output .s-fatal { color: #f85149; font-weight: 600; }
  #output .s-result { color: #f0f6fc; font-size: 0.95rem; margin: 4px 0; }
  #output .s-headline { color: #f0f6fc; font-size: 1.2rem; font-weight: 700; margin: 16px 0 8px; }
  #output .s-kv { color: #8b949e; margin: 2px 0; }
  #output .s-sublist { color: #8b949e; margin: 1px 0; padding-left: 16px; }
  #output .s-prompt { color: #d29922; font-style: italic; }
  #output .s-markdown { color: #c9d1d9; }

  .status-bar {
    display: flex; align-items: center; gap: 8px; margin-bottom: 16px;
    font-size: 0.85rem;
  }
  .indicator { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .indicator.idle { background: #8b949e; }
  .indicator.running { background: #3fb950; animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  details.settings { margin-bottom: 16px; }
  details.settings summary {
    cursor: pointer; color: #8b949e; font-size: 0.8rem; user-select: none;
    padding: 4px 0;
  }
  details.settings summary:hover { color: #c9d1d9; }
  details.settings[open] summary { margin-bottom: 12px; }

  .prompt-overlay {
    display: none; position: fixed; inset: 0; z-index: 100;
    background: rgba(0,0,0,0.6); align-items: center; justify-content: center;
  }
  .prompt-dialog {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 24px; max-width: 500px; width: 90%;
  }
  .prompt-text { color: #d29922; margin-bottom: 12px; font-size: 0.9rem; }
  .prompt-dialog input {
    width: 100%; background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    padding: 10px 12px; color: #c9d1d9; font-size: 0.95rem; outline: none;
    margin-bottom: 12px;
  }
  .prompt-dialog input:focus { border-color: #58a6ff; }
  .prompt-dialog button {
    background: #238636; color: #fff; border: none; border-radius: 6px;
    padding: 8px 20px; font-size: 0.9rem; cursor: pointer;
  }
  .prompt-dialog button:hover { background: #2ea043; }
</style>
</head>
<body>
<div class="sidebar">
  <div class="sidebar-header">
    <a href="https://github.com/day50-dev/agent-cli" target="_blank" class="sidebar-logo-link">
      <img src="/static/logo_face.avif" class="sidebar-logo" alt="">
      <span class="sidebar-brand">maxac</span>
    </a>
  </div>
  <div class="history-list" id="history-list">
    <div class="history-empty">No runs yet.</div>
  </div>
</div>
<div class="main">
  <div class="main-content">
    <div class="status-bar">
      <span class="indicator idle" id="indicator"></span>
      <span id="status-text"></span>
    </div>

    <details class="settings">
    <summary>Skills &amp; Settings ▾</summary>

    <div class="skills-panel">
      <h2>Skills</h2>
      <div class="skill-list" id="skill-list">
        <span class="no-skills">Loading...</span>
      </div>
    </div>

    <div class="config-panel">
      <h2>Model Config</h2>
      <div class="preset-pills">
        <span class="preset-pill" onclick="fillPreset('ollama')">ollama</span>
        <span class="preset-pill" onclick="fillPreset('llamacpp')">llama.cpp</span>
        <span class="preset-pill" onclick="fillPreset('openrouter')">openrouter</span>
      </div>
      <div class="config-row">
        <label>Model</label>
        <input type="text" id="cfg-model" placeholder="e.g. gpt-4o">
      </div>
      <div class="config-row">
        <label>URL</label>
        <input type="text" id="cfg-url" placeholder="e.g. https://api.openai.com/v1">
      </div>
      <div class="config-row">
        <label>Key</label>
        <input type="text" id="cfg-key" placeholder="sk-...">
      </div>
      <div class="config-actions">
        <button class="primary" onclick="saveConfig()">Save</button>
        <span class="config-saved" id="config-saved">Saved</span>
      </div>
    </div>

    </details>

    <div class="task-form">
      <textarea id="task-input" rows="2" placeholder="Describe what you want done..."></textarea>
      <div class="task-actions">
        <button id="run-btn" onclick="runTask()">Run</button>
        <button id="cancel-btn" class="muted" onclick="cancelTask()" disabled>Cancel</button>
      </div>
    </div>

    <div id="output"></div>
  </div>
</div>

<script>
const evtSource = new EventSource('/events');
const output = document.getElementById('output');
const indicator = document.getElementById('indicator');
const statusText = document.getElementById('status-text');
const runBtn = document.getElementById('run-btn');
const cancelBtn = document.getElementById('cancel-btn');
const taskInput = document.getElementById('task-input');
const historyList = document.getElementById('history-list');
let selectedHistoryId = null;
let currentRunEvents = [];
const promptModal = document.getElementById('promptModal');

let hasRealOutput = false;

function showSpinner() {
  output.innerHTML = '<div class="spinner-container"><div class="spinner"></div>Waiting for model...</div>';
  output.classList.add('waiting');
}

function hideSpinner() {
  const sc = output.querySelector('.spinner-container');
  if (sc) sc.remove();
  output.classList.remove('waiting');
}

evtSource.addEventListener('output', (e) => {
  const msg = JSON.parse(e.data);
  currentRunEvents.push(msg);
  if (msg.type === '__state__') {
    if (msg.data === 'running') {
      indicator.className = 'indicator running';
      statusText.textContent = 'Running...';
      runBtn.disabled = true;
      cancelBtn.disabled = false;
      taskInput.disabled = true;
      currentRunEvents = [];
      hasRealOutput = false;
      showSpinner();
      loadHistory();
    } else {
      indicator.className = 'indicator idle';
      statusText.textContent = '';
      runBtn.disabled = false;
      cancelBtn.disabled = true;
      taskInput.disabled = false;
      hideSpinner();
      promptModal.style.display = 'none';
      loadHistory();
    }
    return;
  }
  if (msg.type === '__prompt__') {
    showPrompt(msg.data);
    return;
  }
  if (!hasRealOutput) {
    hasRealOutput = true;
    output.innerHTML = '';
  }
  appendEvent(msg.type, msg.data);
});

evtSource.addEventListener('error', () => {
});

function appendEvent(type, data) {
  const el = document.createElement('div');
  el.className = 's-' + type;
  if (type === 'separator') {
    el.innerHTML = '<hr class="s-separator">';
  } else if (type === 'section' || type === 'headline') {
    el.textContent = data;
  } else if (type === 'markdown') {
    el.textContent = data;
  } else {
    el.textContent = data;
  }
  output.appendChild(el);
  output.scrollTop = output.scrollHeight;
}

function renderEvents(events, container) {
  container.innerHTML = '';
  for (const ev of events) {
    if (ev.type === '__state__') continue;
    appendEventTo(ev.type, ev.data, container);
  }
  container.scrollTop = container.scrollHeight;
}

function appendEventTo(type, data, container) {
  const el = document.createElement('div');
  el.className = 's-' + type;
  if (type === 'separator') {
    el.innerHTML = '<hr class="s-separator">';
  } else if (type === 'section' || type === 'headline') {
    el.textContent = data;
  } else if (type === 'markdown') {
    el.textContent = data;
  } else {
    el.textContent = data;
  }
  container.appendChild(el);
}

function runTask() {
  const task = taskInput.value.trim();
  if (!task) return;
  hasRealOutput = false;
  currentRunEvents = [];
  showSpinner();
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/run', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.send(JSON.stringify({ task: task }));
}

function cancelTask() {
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/cancel', true);
  xhr.send();
}

function replayTask(id) {
  fetch('/history/' + id).then(r => r.json()).then(entry => {
    taskInput.value = entry.task;
    runTask();
  });
}

function selectHistory(id) {
  selectedHistoryId = id;
  document.querySelectorAll('.history-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.id) === id);
  });
  hideSpinner();
  fetch('/history/' + id).then(r => r.json()).then(entry => {
    taskInput.value = entry.task;
    const events = entry.events || [];
    output.innerHTML = '';
    for (const ev of events) {
      appendEvent(ev.type, ev.data);
    }
    taskInput.focus();
  });
}

function loadHistory() {
  fetch('/history').then(r => r.json()).then(list => {
    if (list.length === 0) {
      historyList.innerHTML = '<div class="history-empty">No runs yet.</div>';
      return;
    }
    historyList.innerHTML = list.map(h => {
      const active = h.id === selectedHistoryId ? 'active' : '';
      return `<div class="history-item ${active}" data-id="${h.id}" onclick="selectHistory(${h.id})">
        <div class="h-task">${htmlEncode(h.task)}</div>
        <div class="h-meta">
          <span class="h-indicator ${h.status}"></span>${h.time}
        </div>
        <div style="margin-top:4px">
          <span class="preset-pill" onclick="event.stopPropagation();replayTask(${h.id})">replay</span>
        </div>
      </div>`;
    }).join('');
    // Re-select currently selected item if still visible
    if (selectedHistoryId && list.some(h => h.id === selectedHistoryId)) {
      const el = document.querySelector(`.history-item[data-id="${selectedHistoryId}"]`);
      if (el) el.classList.add('active');
    }
  });
}

const PRESETS = {
  ollama: { url: 'http://localhost:11434/v1', model: '', key: '' },
  llamacpp: { url: 'http://localhost:8080/v1', model: '', key: '' },
  openrouter: { url: 'https://openrouter.ai/api/v1', model: '', key: '' },
};

function fillPreset(name) {
  const p = PRESETS[name];
  if (!p) return;
  document.getElementById('cfg-model').value = p.model;
  document.getElementById('cfg-url').value = p.url;
  document.getElementById('cfg-key').value = p.key;
}

function useSkill(name) {
  const tag = 'skill:' + name.replace(/\\s+/g, '-');
  if (taskInput.value.trim() && !taskInput.value.trim().endsWith(tag)) {
    taskInput.value = taskInput.value.trim() + ' ' + tag;
  } else {
    taskInput.value = tag;
  }
  taskInput.focus();
}

function saveConfig() {
  const data = {
    model: document.getElementById('cfg-model').value.trim(),
    url: document.getElementById('cfg-url').value.trim(),
    key: document.getElementById('cfg-key').value.trim(),
  };
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/config', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = () => {
    if (xhr.status === 200) {
      const cfg = JSON.parse(xhr.responseText);
      document.getElementById('cfg-model').value = cfg.model;
      document.getElementById('cfg-url').value = cfg.url;
      document.getElementById('cfg-key').value = cfg.key;
      const saved = document.getElementById('config-saved');
      saved.style.display = 'inline';
      setTimeout(() => { saved.style.display = 'none'; }, 2000);
    }
  };
  xhr.send(JSON.stringify(data));
}

function loadConfig() {
  fetch('/config').then(r => r.json()).then(cfg => {
    document.getElementById('cfg-model').value = cfg.model || '';
    document.getElementById('cfg-url').value = cfg.url || '';
    document.getElementById('cfg-key').value = cfg.key || '';
  });
}

loadConfig();
loadHistory();
fetch('/skills').then(r => r.json()).then(skills => {
  const container = document.getElementById('skill-list');
  if (skills.length === 0) {
    container.innerHTML = '<span class="no-skills">No saved skills yet.</span>';
    return;
  }
  container.innerHTML = skills.map(s =>
    `<span class="skill-tag" title="${htmlEncode(s.description || '')}" onclick="useSkill('${htmlEncode(s.name)}')">${htmlEncode(s.name)}</span>`
  ).join('');
}).catch(() => {
  document.getElementById('skill-list').innerHTML = '<span class="no-skills">Failed to load skills.</span>';
});

function htmlEncode(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function showPrompt(text) {
  document.getElementById('prompt-text').textContent = text;
  const input = document.getElementById('prompt-input');
  input.value = '';
  input.focus();
  promptModal.style.display = 'flex';
}

function sendPromptResponse() {
  const input = document.getElementById('prompt-input');
  const val = input.value;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/prompt-response', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = () => { promptModal.style.display = 'none'; };
  xhr.send(JSON.stringify({ response: val }));
}

document.getElementById('prompt-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendPromptResponse();
});
</script>

<div class="prompt-overlay" id="promptModal">
  <div class="prompt-dialog">
    <div class="prompt-text" id="prompt-text"></div>
    <input type="text" id="prompt-input" placeholder="Type your response...">
    <button onclick="sendPromptResponse()">Submit</button>
  </div>
</div>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Single-file HTTP handler for the serve interface."""

    # Suppress default logging per-request (we do our own)
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code: int, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html_text: str):
        body = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self):
        static_dir = Path(__file__).resolve().parent.parent / "static"
        rel_path = self.path.lstrip("/")
        file_path = static_dir / Path(rel_path).name
        if not file_path.exists() or not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = file_path.read_bytes()
        ext = file_path.suffix.lower()
        mime = {
            ".avif": "image/avif",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ----

    def do_GET(self):
        if self.path == "/":
            self._send_html(200, _HTML_PAGE)
        elif self.path == "/events":
            self._handle_sse()
        elif self.path == "/skills":
            skills = _agent.get_available_skills() if _agent else []
            self._send_json(200, skills)
        elif self.path == "/config":
            self._handle_get_config()
        elif self.path.startswith("/history/"):
            self._handle_get_history_detail()
        elif self.path == "/history":
            self._handle_get_history()
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif self.path.startswith("/static/"):
            self._serve_static()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/run":
            self._handle_run()
        elif self.path == "/cancel":
            self._handle_cancel()
        elif self.path == "/config":
            self._handle_set_config()
        elif self.path == "/prompt-response":
            self._handle_prompt_response()
        else:
            self._send_json(404, {"error": "not found"})

    # ---- SSE ----

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Drain any stale events from previous runs
        while not _event_queue.empty():
            try:
                _event_queue.get_nowait()
            except queue.Empty:
                break

        _server_ready.set()

        try:
            while not _server_stop.is_set():
                try:
                    ev = _event_queue.get(timeout=1)
                    msg = _sse_message(ev["type"], ev["data"])
                    self.wfile.write(msg)
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---- config ----

    def _handle_get_config(self):
        cfg = _agent.model_config if _agent else {}
        self._send_json(200, {
            "model": cfg.get("model") or "",
            "url": cfg.get("url") or "",
            "key": cfg.get("key") or "",
        })

    def _handle_set_config(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._send_json(400, {"error": "empty request body"})
            return
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        for key in ("model", "url", "key"):
            if key in data:
                _agent.set_model_config(key, data[key].strip() if data[key] else "")

        self._handle_get_config()

    def _handle_prompt_response(self):
        global _prompt_request, _prompt_response
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._send_json(400, {"error": "empty request body"})
            return
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return
        _prompt_response = (data.get("response") or "").strip()
        _prompt_request.set()
        self._send_json(200, {"status": "ok"})

    # ---- history ----

    def _handle_get_history(self):
        with _history_lock:
            summary = [
                {"id": e["id"], "task": e["task"], "time": e["time"], "status": e["status"]}
                for e in _history
            ]
        self._send_json(200, summary)

    def _handle_get_history_detail(self):
        try:
            run_id = int(self.path.split("/")[-1])
        except (ValueError, IndexError):
            self._send_json(400, {"error": "invalid id"})
            return
        with _history_lock:
            for entry in _history:
                if entry["id"] == run_id:
                    self._send_json(200, entry)
                    return
        self._send_json(404, {"error": "not found"})

    # ---- run task ----

    def _handle_run(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._send_json(400, {"error": "empty request body"})
            return
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        task = (data.get("task") or "").strip()
        if not task:
            self._send_json(400, {"error": "missing 'task' field"})
            return

        if _task_running.is_set():
            self._send_json(409, {"error": "a task is already running"})
            return

        self._send_json(200, {"status": "started"})

        global _run_id_counter, _current_run_id, _current_run_events
        _run_id_counter += 1
        run_id = _run_id_counter
        _current_run_id = run_id
        _current_run_events = []
        history_entry = {
            "id": run_id,
            "task": task,
            "time": time.strftime("%H:%M:%S"),
            "status": "running",
            "events": _current_run_events,
        }
        with _history_lock:
            _history.insert(0, history_entry)
        _save_history()

        _task_running.set()
        # Broadcast running state
        try:
            _event_queue.put_nowait({"type": "__state__", "data": "running"})
        except queue.Full:
            _event_queue.put({"type": "__state__", "data": "running"}, timeout=5)

        t = threading.Thread(target=_run_task, args=(task, run_id), daemon=True)
        t.start()

    # ---- cancel ----

    def _handle_cancel(self):
        # Halt the running asyncio loop by raising an interrupt
        old_task = _task_running
        if old_task.is_set():
            old_task.clear()
            # The next event loop iteration will detect this
        self._send_json(200, {"status": "cancelled"})


# ---------------------------------------------------------------------------
# Interactive prompt handling (replaces sys.stdin for web)
# ---------------------------------------------------------------------------

class _PromptPipe:
    """File-like object that replaces sys.stdin during web task runs.

    Each read blocks until the browser sends a response via the API.
    """

    def __init__(self):
        self._buf = ""

    def readline(self, size=-1):
        global _prompt_request, _prompt_response
        _prompt_request.clear()
        _prompt_response = None
        # Signal browser — already done via WebOutput.prompt + SSE
        _prompt_request.wait()
        ans = _prompt_response or ""
        _prompt_response = None
        self._buf = ans + "\n"
        return self._buf

    def close(self):
        pass

    def fileno(self):
        raise OSError("no fileno")


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------

def _run_task(task: str, run_id: int):
    """Run a task in a background thread using its own asyncio loop."""
    global _current_run_id, _current_run_events
    old_stdin = sys.stdin
    sys.stdin = _PromptPipe()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_execute_and_stream(task))
    finally:
        sys.stdin = old_stdin
        loop.close()
        _task_running.clear()
        # Finalize history entry
        with _history_lock:
            for entry in _history:
                if entry["id"] == run_id:
                    entry["status"] = "done"
                    break
        _save_history()
        _current_run_id = None
        _current_run_events = None
        # Broadcast idle state
        try:
            _event_queue.put_nowait({"type": "__state__", "data": "idle"})
        except queue.Full:
            try:
                _event_queue.put({"type": "__state__", "data": "idle"}, timeout=5)
            except queue.Full:
                pass


async def _execute_and_stream(task: str):
    """Execute the task and stream output via the event queue."""
    out = _agent.out
    try:
        await _agent.execute_task(task)
    except SystemExit:
        # _agent calls sys.exit(1) on various failures
        # Don't let that kill the server
        pass
    except Exception as e:
        out.fatal(f"task error: {e}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def serve(config_dir: Path, mcp_file = None, auto_yes: bool = False, verbose: int = 0, host: str = "127.0.0.1", port: int = 8080):
    """Start the web interface server."""
    global _web_server, _agent, _event_queue, _task_running, _server_ready, _history_path

    _event_queue = queue.Queue(maxsize=5000)

    _history_path = config_dir / "history.json"
    _load_history()

    _web_output = WebOutput(
        _event_queue,
        level=Output.DEBUG if verbose >= 2 else (Output.INFO if verbose >= 1 else Output.WARN),
    )

    _agent = AgentCLI(
        config_dir=config_dir,
        auto_yes=auto_yes,
        verbose=verbose,
        mcp_file=mcp_file,
    )
    _agent.out = _web_output

    for attempt in range(100):
        try:
            _server = ThreadingHTTPServer((host, port), _Handler)
            break
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                port += 1
            else:
                raise
    else:
        raise RuntimeError("Could not find an available port after 100 attempts")
    _web_server = _server

    print(f"maxac serve — http://{host}:{port}")
    print("Press Ctrl+C to stop.")

    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        _server_stop.set()
        _server.server_close()
