"""
Builds an HTML dashboard listing all Claude Code agents, skills, and MCP servers.

Scans:
  ~/.claude/agents/                       -> user-defined agents
  ~/.claude/skills/                       -> user-defined skills
  ~/.claude/plugins/marketplaces/**/      -> plugin agents and skills (installed via marketplaces)
  ~/.claude.json                          -> enabled plugins + MCP servers
  ~/.claude/settings.json                 -> MCP servers (alternative location)

Output:
  ~/.claude/registry/dashboard.html
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
OUTPUT = CLAUDE_DIR / "registry" / "dashboard.html"
TRANSLATIONS_FILE = CLAUDE_DIR / "registry" / "translations.json"


def load_translations() -> dict:
    if not TRANSLATIONS_FILE.is_file():
        return {"agents": {}, "skills": {}}
    try:
        data = json.loads(TRANSLATIONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"agents": {}, "skills": {}}
    return {
        "agents": data.get("agents") or {},
        "skills": data.get("skills") or {},
    }


def shorten_description(text: str, limit: int = 280) -> str:
    """Trim long YAML block descriptions to a single readable sentence/paragraph."""
    text = (text or "").strip()
    if not text:
        return ""
    # Cut off Examples blocks and similar noise.
    for marker in ("\n\nExamples:", "\nExamples:", "\n\n<example>", "\n<example>"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx].strip()
            break
    # Collapse internal whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


TRANSLATIONS = load_translations()


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    """Extract YAML-ish frontmatter and body from a markdown file. Returns ({}, body) if no frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""

    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")

    fm: dict = {}
    current_key = None
    block_mode = False  # True when value is a YAML literal/folded scalar (| or >)
    block_lines: list[str] = []
    block_indent = 0

    def flush_block() -> None:
        nonlocal block_mode, block_lines, block_indent
        if current_key and block_mode:
            fm[current_key] = "\n".join(block_lines).strip()
        block_mode = False
        block_lines = []
        block_indent = 0

    lines = raw.splitlines()
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)

        if m and (not block_mode or not line.startswith(" ")):
            flush_block()
            current_key = m.group(1)
            value = m.group(2).strip()
            if value in ("|", ">", "|-", ">-", "|+", ">+"):
                block_mode = True
                block_lines = []
                block_indent = 0
                continue
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            fm[current_key] = value
            continue

        if block_mode:
            if not stripped:
                block_lines.append("")
                continue
            indent = len(line) - len(line.lstrip(" "))
            if block_indent == 0:
                block_indent = indent
            if indent < block_indent and stripped:
                # End of block; reprocess this line as a new key if applicable.
                flush_block()
                if m:
                    current_key = m.group(1)
                    value = m.group(2).strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    fm[current_key] = value
                continue
            block_lines.append(line[block_indent:] if indent >= block_indent else stripped)
            continue

        if not stripped or stripped.startswith("#"):
            continue
        if current_key and line.startswith((" ", "\t")):
            fm[current_key] = (fm.get(current_key, "") + " " + stripped).strip()

    flush_block()
    return fm, body


def collect_agents() -> list[dict]:
    out: list[dict] = []
    user_dir = CLAUDE_DIR / "agents"
    if user_dir.is_dir():
        for f in sorted(user_dir.glob("*.md")):
            fm, body = parse_frontmatter(f)
            out.append(_agent_record(fm, body, f, source="user", source_label="User"))

    plugins_root = CLAUDE_DIR / "plugins" / "marketplaces"
    if plugins_root.is_dir():
        for agent_md in sorted(plugins_root.glob("**/agents/*.md")):
            plugin_name = _plugin_name_from_path(agent_md)
            fm, body = parse_frontmatter(agent_md)
            out.append(
                _agent_record(
                    fm, body, agent_md, source="plugin", source_label=f"Plugin: {plugin_name}"
                )
            )
    return out


def _plugin_name_from_path(path: Path) -> str:
    parts = path.parts
    try:
        idx = parts.index("plugins")
        # second occurrence is the plugin folder name
        idx2 = parts.index("plugins", idx + 1)
        return parts[idx2 + 1]
    except ValueError:
        return path.parent.parent.name


def _agent_record(fm: dict, body: str, path: Path, source: str, source_label: str) -> dict:
    name = fm.get("name") or path.stem
    raw_desc = fm.get("description", "").strip()
    translation = TRANSLATIONS["agents"].get(name)
    if isinstance(translation, dict):
        display_name = translation.get("display_name") or name
        description = translation.get("description") or shorten_description(raw_desc)
    elif isinstance(translation, str) and translation:
        display_name = name
        description = translation
    else:
        display_name = name
        description = shorten_description(raw_desc)
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "description_en": shorten_description(raw_desc) if translation else "",
        "tools": [t.strip() for t in re.split(r"[,\s]+", fm.get("tools", "")) if t.strip()],
        "model": fm.get("model", "inherit"),
        "source": source,
        "source_label": source_label,
        "path": str(path),
        "body_preview": body.strip()[:300],
    }


def collect_skills() -> list[dict]:
    out: list[dict] = []
    user_dir = CLAUDE_DIR / "skills"
    if user_dir.is_dir():
        for skill_md in sorted(user_dir.glob("**/SKILL.md")):
            fm, body = parse_frontmatter(skill_md)
            out.append(_skill_record(fm, body, skill_md, "user", "User"))

    plugins_root = CLAUDE_DIR / "plugins" / "marketplaces"
    if plugins_root.is_dir():
        for skill_md in sorted(plugins_root.glob("**/skills/**/SKILL.md")):
            plugin_name = _plugin_name_from_path(skill_md)
            fm, body = parse_frontmatter(skill_md)
            out.append(_skill_record(fm, body, skill_md, "plugin", f"Plugin: {plugin_name}"))
    return out


def _skill_record(fm: dict, body: str, path: Path, source: str, source_label: str) -> dict:
    name = fm.get("name") or path.parent.name
    raw_desc = fm.get("description", "").strip()
    translation = TRANSLATIONS["skills"].get(name)
    if isinstance(translation, dict):
        display_name = translation.get("display_name") or name
        description = translation.get("description") or shorten_description(raw_desc)
    elif isinstance(translation, str) and translation:
        display_name = name
        description = translation
    else:
        display_name = name
        description = shorten_description(raw_desc)
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "description_en": shorten_description(raw_desc) if translation else "",
        "source": source,
        "source_label": source_label,
        "path": str(path),
        "body_preview": body.strip()[:300],
    }


def collect_mcp() -> tuple[list[dict], list[str]]:
    """Return (servers, notes)."""
    servers: list[dict] = []
    notes: list[str] = []

    claude_json = HOME / ".claude.json"
    if claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            for name, cfg in (data.get("mcpServers") or {}).items():
                servers.append(
                    {"name": name, "scope": "global", "config": cfg, "source": "~/.claude.json"}
                )
        except (json.JSONDecodeError, OSError):
            notes.append("Could not parse ~/.claude.json")

    settings = CLAUDE_DIR / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            for name, cfg in (data.get("mcpServers") or {}).items():
                servers.append(
                    {"name": name, "scope": "global", "config": cfg, "source": "settings.json"}
                )
        except (json.JSONDecodeError, OSError):
            notes.append("Could not parse settings.json")

    if not servers:
        notes.append(
            "No locally configured MCP servers. MCP servers visible inside Claude Code "
            "(PubMed, bioRxiv, Clinical Trials, Notion, etc.) are managed via the "
            "claude.ai web interface, not via local config."
        )
    return servers, notes


def collect_enabled_plugins() -> list[str]:
    claude_json = HOME / ".claude.json"
    if not claude_json.is_file():
        return []
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    enabled = data.get("enabledPlugins") or {}
    return sorted(enabled.keys()) if isinstance(enabled, dict) else []


def render_html(agents, skills, mcp_servers, mcp_notes, enabled_plugins) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    my_agents = [a for a in agents if a["source"] == "user"]
    plugin_agents = [a for a in agents if a["source"] == "plugin"]
    my_skills = [s for s in skills if s["source"] == "user"]
    plugin_skills = [s for s in skills if s["source"] == "plugin"]
    plugins_count = len(plugin_agents) + len(plugin_skills)

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Claude Code Registry</title>
<style>
  :root {{
    --bg: #0f1115; --panel: #171a21; --border: #262a33;
    --text: #e6e6e6; --muted: #8b95a7; --accent: #6ea8fe;
    --user: #4ade80; --plugin: #f59e0b; --inherit: #94a3b8;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 14px/1.5 -apple-system, Segoe UI, system-ui, sans-serif; background: var(--bg); color: var(--text); }}
  header {{ padding: 18px 28px; border-bottom: 1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }}
  header h1 {{ margin: 0; font-size: 18px; font-weight: 600; }}
  header .meta {{ color: var(--muted); font-size: 12px; }}
  .tabs {{ display:flex; gap:4px; padding: 12px 28px 0; border-bottom: 1px solid var(--border); }}
  .tab {{ padding: 8px 14px; cursor: pointer; border-radius: 6px 6px 0 0; color: var(--muted); user-select:none; }}
  .tab.active {{ background: var(--panel); color: var(--text); border: 1px solid var(--border); border-bottom: 1px solid var(--panel); margin-bottom: -1px; }}
  .tab .count {{ display:inline-block; margin-left:6px; font-size:11px; padding:1px 7px; border-radius:10px; background:#2a3040; color:var(--muted); }}
  .controls {{ padding: 14px 28px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
  .controls input[type=text] {{ flex: 1; min-width: 220px; padding: 8px 12px; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font: inherit; }}
  .controls select {{ padding: 8px 10px; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font: inherit; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; padding: 8px 28px 32px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }}
  .card h3 {{ margin: 0 0 6px; font-size: 14px; display: flex; align-items: center; gap:8px; flex-wrap:wrap; }}
  .badge {{ font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 600; letter-spacing: 0.3px; text-transform: uppercase; }}
  .badge.user {{ background: rgba(74,222,128,0.15); color: var(--user); }}
  .badge.plugin {{ background: rgba(245,158,11,0.15); color: var(--plugin); }}
  .badge.model {{ background: rgba(110,168,254,0.15); color: var(--accent); }}
  .desc {{ color: var(--muted); margin: 6px 0 10px; font-size: 13px; }}
  .tools {{ display: flex; gap: 4px; flex-wrap: wrap; }}
  .tool {{ font-size: 11px; padding: 2px 8px; background: #232733; border: 1px solid var(--border); border-radius: 4px; color: #c5cbd6; }}
  .tool.all {{ background: rgba(245,158,11,0.1); color: var(--plugin); border-color: rgba(245,158,11,0.3); }}
  .meta-row {{ font-size: 11px; color: var(--muted); margin-top: 10px; word-break: break-all; }}
  .panel {{ display: none; }}
  .panel.active {{ display: block; }}
  .empty {{ padding: 40px; text-align: center; color: var(--muted); }}
  details {{ margin-top: 10px; }}
  details summary {{ cursor: pointer; color: var(--accent); font-size: 12px; }}
  .desc-en {{ color: #6b7280; font-size: 11px; font-style: italic; margin: 4px 0 8px; }}
  .alias {{ color: var(--muted); font-size: 11px; font-family: ui-monospace, Consolas, monospace; margin: -4px 0 6px; }}
  .plugin-section {{ padding: 8px 28px 18px; }}
  .plugin-section > h2 {{ margin: 16px 0 10px; font-size: 14px; color: var(--plugin); display:flex; align-items:center; gap:10px; }}
  .plugin-section > h2 .count {{ font-size: 11px; color: var(--muted); font-weight: 400; }}
  .plugin-section > .grid {{ padding: 0; }}
  pre {{ background: #0c0e12; border: 1px solid var(--border); border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 12px; white-space: pre-wrap; }}
  .actions {{ display:flex; gap:6px; margin-top:10px; }}
  .btn {{ font: inherit; font-size: 12px; padding: 6px 12px; background: var(--accent); color: #0b1220; border: none; border-radius: 5px; cursor: pointer; font-weight: 600; }}
  .btn:hover {{ filter: brightness(1.1); }}
  .btn.secondary {{ background: #2a3040; color: var(--text); }}
  .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .server-status {{ font-size: 12px; padding: 4px 10px; border-radius: 12px; }}
  .server-status.ok {{ background: rgba(74,222,128,0.15); color: var(--user); }}
  .server-status.off {{ background: rgba(239,68,68,0.15); color: #f87171; }}
  .modal-bg {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 100; align-items: center; justify-content: center; padding: 20px; }}
  .modal-bg.open {{ display: flex; }}
  .modal {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 22px; width: 100%; max-width: 720px; max-height: 90vh; overflow-y: auto; }}
  .modal h2 {{ margin: 0 0 4px; font-size: 16px; }}
  .modal .muted {{ color: var(--muted); font-size: 12px; margin-bottom: 14px; }}
  .modal textarea {{ width: 100%; min-height: 120px; background: #0c0e12; color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 10px; font: inherit; font-size: 13px; resize: vertical; }}
  .modal .row {{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }}
  .modal .result {{ margin-top: 14px; }}
  .modal .result pre {{ max-height: 400px; overflow-y: auto; }}
  .spinner {{ display: inline-block; width: 12px; height: 12px; border: 2px solid var(--muted); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; margin-right: 6px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .progress {{ display: none; margin: 12px 0 8px; }}
  .progress.active {{ display: block; }}
  .progress-bar {{ width: 100%; height: 8px; background: #2a3040; border-radius: 4px; overflow: hidden; }}
  .progress-fill {{ width: 0%; height: 100%; background: linear-gradient(90deg, var(--accent), #a78bfa); border-radius: 4px; transition: width 0.3s linear; }}
  .progress-fill.indeterminate {{ width: 35% !important; background: linear-gradient(90deg, transparent, var(--accent), transparent); animation: slide 1.4s ease-in-out infinite; }}
  @keyframes slide {{ 0% {{ margin-left: -35%; }} 100% {{ margin-left: 100%; }} }}
  .progress-meta {{ display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 6px; font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Claude Code Registry</h1>
    <div class="meta">Сгенерировано: {now}</div>
  </div>
  <div style="display:flex; gap:12px; align-items:center;">
    <span class="meta">Включённых плагинов: {len(enabled_plugins)}</span>
    <span id="server-status" class="server-status off">Сервер: проверяю…</span>
  </div>
</header>

<div class="tabs">
  <div class="tab active" data-tab="my-agents">Мои агенты <span class="count">{len(my_agents)}</span></div>
  <div class="tab" data-tab="my-skills">Мои скиллы <span class="count">{len(my_skills)}</span></div>
  <div class="tab" data-tab="plugins">Плагины <span class="count">{plugins_count}</span></div>
  <div class="tab" data-tab="mcp">MCP <span class="count">{len(mcp_servers)}</span></div>
</div>

<div class="controls">
  <input type="text" id="search" placeholder="Поиск по имени или описанию...">
</div>

<div id="my-agents-panel" class="panel active">
  <div class="grid">
    {render_agents(my_agents)}
  </div>
</div>

<div id="my-skills-panel" class="panel">
  <div class="grid">
    {render_skills(my_skills)}
  </div>
</div>

<div id="plugins-panel" class="panel">
  {render_plugins(plugin_agents, plugin_skills)}
</div>

<div id="mcp-panel" class="panel">
  <div class="grid">
    {render_mcp(mcp_servers, mcp_notes)}
  </div>
</div>

<div id="modal-bg" class="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h2 id="modal-title">Запустить агента</h2>
    <div class="muted" id="modal-desc"></div>
    <label class="muted" style="display:block; margin-bottom:6px;">Что нужно сделать (этот текст уйдёт агенту):</label>
    <textarea id="modal-prompt" placeholder="Например: скачай фото для артикула 151339620"></textarea>
    <div class="row">
      <button class="btn secondary" onclick="closeModal()">Отмена</button>
      <button class="btn" id="modal-run" onclick="runAgentFromModal()">Запустить</button>
    </div>
    <div class="progress" id="modal-progress">
      <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
      <div class="progress-meta">
        <span id="progress-status">Запускаю агента…</span>
        <span id="progress-time">0:00</span>
      </div>
    </div>
    <div class="result" id="modal-result" style="display:none;">
      <div class="muted" id="modal-result-status"></div>
      <div id="modal-result-paths"></div>
      <div id="modal-result-actions" style="display:none; margin-top: 8px;">
        <button class="btn secondary" onclick="downloadResultTxt()">📄 Скачать TXT</button>
        <button class="btn secondary" id="btn-pdf" onclick="downloadResultPdf()">📕 Скачать PDF</button>
      </div>
      <pre id="modal-result-text"></pre>
    </div>
  </div>
</div>

<script>
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.panel');
  tabs.forEach(t => t.addEventListener('click', () => {{
    tabs.forEach(x => x.classList.remove('active'));
    panels.forEach(p => p.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.tab + '-panel').classList.add('active');
  }}));

  const search = document.getElementById('search');

  function applyFilter() {{
    const q = search.value.toLowerCase();
    document.querySelectorAll('.card').forEach(card => {{
      const text = card.dataset.search || '';
      card.style.display = (!q || text.includes(q)) ? '' : 'none';
    }});
    // Hide plugin section headers whose cards are all hidden
    document.querySelectorAll('.plugin-section').forEach(sec => {{
      const visible = Array.from(sec.querySelectorAll('.card')).some(c => c.style.display !== 'none');
      sec.style.display = visible ? '' : 'none';
    }});
  }}
  search.addEventListener('input', applyFilter);

  // Server health check (degrades gracefully if launcher server is not running).
  const SERVER_BASE = (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin
    : 'http://127.0.0.1:8765';
  const statusEl = document.getElementById('server-status');
  let serverOk = false;
  fetch(SERVER_BASE + '/api/health')
    .then(r => r.json())
    .then(d => {{ serverOk = !!d.ok; updateServerUI(); }})
    .catch(() => {{ serverOk = false; updateServerUI(); }});

  function updateServerUI() {{
    if (serverOk) {{
      statusEl.textContent = 'Сервер: онлайн';
      statusEl.className = 'server-status ok';
      document.querySelectorAll('.btn[data-needs-server]').forEach(b => {{
        b.disabled = false;
        b.title = '';
      }});
    }} else {{
      statusEl.textContent = 'Сервер выключен — запустите server.py';
      statusEl.className = 'server-status off';
      document.querySelectorAll('.btn[data-needs-server]').forEach(b => {{
        b.disabled = true;
        b.title = 'Сначала запустите python ~/.claude/registry/server.py';
      }});
    }}
  }}

  // Re-poll health every 15s so the dashboard auto-recovers when the server comes back.
  setInterval(() => {{
    fetch(SERVER_BASE + '/api/health')
      .then(r => r.json())
      .then(d => {{
        const next = !!d.ok;
        if (next !== serverOk) {{ serverOk = next; updateServerUI(); }}
      }})
      .catch(() => {{
        if (serverOk) {{ serverOk = false; updateServerUI(); }}
      }});
  }}, 15000);

  let currentAgent = null;
  function openAgentModal(name, displayName, desc) {{
    if (!serverOk) {{ alert('Сервер не запущен. Запустите python ~/.claude/registry/server.py и обновите страницу.'); return; }}
    currentAgent = name;
    const title = displayName && displayName !== name ? displayName + ' (' + name + ')' : name;
    document.getElementById('modal-title').textContent = 'Запустить: ' + title;
    document.getElementById('modal-desc').textContent = desc || '';
    document.getElementById('modal-prompt').value = '';
    document.getElementById('modal-result').style.display = 'none';
    document.getElementById('modal-bg').classList.add('open');
    setTimeout(() => document.getElementById('modal-prompt').focus(), 50);
  }}
  function closeModal() {{
    document.getElementById('modal-bg').classList.remove('open');
    currentAgent = null;
  }}
  function formatMmSs(ms) {{
    const s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }}

  let progressTimer = null;
  function startProgress(estimatedMs) {{
    const box = document.getElementById('modal-progress');
    const fill = document.getElementById('progress-fill');
    const statusEl = document.getElementById('progress-status');
    const timeEl = document.getElementById('progress-time');
    box.classList.add('active');
    fill.style.transition = 'none';
    fill.style.width = '0%';
    fill.classList.remove('indeterminate');
    void fill.offsetWidth;  // reflow

    const startedAt = Date.now();
    if (estimatedMs && estimatedMs > 0) {{
      // Determinate: ramp from 0 to 95% over estimatedMs.
      fill.style.transition = 'width ' + (estimatedMs / 1000) + 's linear';
      fill.style.width = '95%';
      statusEl.textContent = 'Идёт работа…';
      timeEl.textContent = '0:00 / ~' + formatMmSs(estimatedMs);
    }} else {{
      fill.classList.add('indeterminate');
      statusEl.textContent = 'Идёт работа (первый запуск, оценки времени нет)…';
      timeEl.textContent = '0:00';
    }}

    progressTimer = setInterval(() => {{
      const elapsed = Date.now() - startedAt;
      if (estimatedMs && estimatedMs > 0) {{
        timeEl.textContent = formatMmSs(elapsed) + ' / ~' + formatMmSs(estimatedMs);
        if (elapsed > estimatedMs * 1.05) {{
          statusEl.textContent = 'Дольше обычного, но работает…';
        }}
      }} else {{
        timeEl.textContent = formatMmSs(elapsed);
      }}
    }}, 500);
  }}

  function endProgress(success) {{
    clearInterval(progressTimer);
    progressTimer = null;
    const box = document.getElementById('modal-progress');
    const fill = document.getElementById('progress-fill');
    fill.classList.remove('indeterminate');
    if (success) {{
      fill.style.transition = 'width 0.3s ease-out';
      fill.style.width = '100%';
      setTimeout(() => box.classList.remove('active'), 600);
    }} else {{
      box.classList.remove('active');
    }}
  }}

  async function runAgentFromModal() {{
    const prompt = document.getElementById('modal-prompt').value.trim();
    if (!prompt) {{ alert('Введите задачу для агента'); return; }}
    const btn = document.getElementById('modal-run');
    const resultBox = document.getElementById('modal-result');
    const statusBox = document.getElementById('modal-result-status');
    const textBox = document.getElementById('modal-result-text');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Работает…';
    resultBox.style.display = 'none';
    textBox.textContent = '';

    let estimatedMs = 0;
    try {{
      const sr = await fetch(SERVER_BASE + '/api/run-stats?name=' + encodeURIComponent(currentAgent));
      const sd = await sr.json();
      if (sd.ok && sd.median_ms) estimatedMs = sd.median_ms;
    }} catch (e) {{}}
    startProgress(estimatedMs);

    try {{
      const r = await fetch(SERVER_BASE + '/api/run-agent', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name: currentAgent, prompt: prompt }})
      }});
      const data = await r.json();
      const actionsBox = document.getElementById('modal-result-actions');
      resultBox.style.display = 'block';
      if (data.ok) {{
        endProgress(true);
        const dur = data.duration_ms ? ' за ' + formatMmSs(data.duration_ms) : '';
        statusBox.textContent = 'Готово' + dur + '.';
        textBox.textContent = data.result || '(пустой ответ)';
        renderResultPaths(data.result || '');
        actionsBox.style.display = (data.result || '').trim() ? 'block' : 'none';
      }} else {{
        endProgress(false);
        statusBox.textContent = 'Ошибка: ' + (data.error || 'неизвестная');
        textBox.textContent = (data.stderr || '') + '\\n' + (data.stdout || '');
        renderResultPaths('');
        actionsBox.style.display = 'none';
      }}
    }} catch (e) {{
      endProgress(false);
      resultBox.style.display = 'block';
      statusBox.textContent = 'Сетевая ошибка';
      textBox.textContent = String(e);
    }} finally {{
      btn.disabled = false;
      btn.textContent = 'Запустить';
    }}
  }}

  function extractPaths(text) {{
    if (!text) return [];
    const found = new Set();
    // Backtick-quoted Windows paths.
    const reBt = /`([A-Za-z]:[\\\\/][^`\\n]+?)`/g;
    let m;
    while ((m = reBt.exec(text)) !== null) {{
      found.add(m[1].replace(/[\\\\/]+$/, ''));
    }}
    // Bare Windows paths (loose) — letters/digits/underscores/dots/dashes/spaces, plus Cyrillic
    const reBare = /([A-Za-z]:[\\\\/](?:[\\w\\u0400-\\u04FF .\\-]+[\\\\/])+[\\w\\u0400-\\u04FF .\\-]*)/g;
    while ((m = reBare.exec(text)) !== null) {{
      found.add(m[1].replace(/[\\\\/]+$/, ''));
    }}
    return Array.from(found);
  }}

  function renderResultPaths(text) {{
    const box = document.getElementById('modal-result-paths');
    box.innerHTML = '';
    const paths = extractPaths(text);
    if (paths.length === 0) return;
    paths.forEach(p => {{
      const btn = document.createElement('button');
      btn.className = 'btn secondary';
      btn.style.marginRight = '6px';
      btn.style.marginBottom = '6px';
      btn.textContent = '📂 Открыть: ' + p;
      btn.onclick = () => openPath(p, btn);
      box.appendChild(btn);
    }});
  }}

  async function openPath(p, btn) {{
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Открываю…';
    try {{
      const r = await fetch(SERVER_BASE + '/api/open-path', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ path: p }})
      }});
      const data = await r.json();
      if (data.ok) {{
        btn.textContent = '✓ ' + p;
        setTimeout(() => {{ btn.textContent = orig; btn.disabled = false; }}, 1500);
      }} else {{
        btn.textContent = '✗ ' + (data.error || 'ошибка');
        setTimeout(() => {{ btn.textContent = orig; btn.disabled = false; }}, 3000);
      }}
    }} catch (e) {{
      btn.textContent = '✗ сетевая ошибка';
      setTimeout(() => {{ btn.textContent = orig; btn.disabled = false; }}, 3000);
    }}
  }}

  function getResultExportTitle() {{
    const text = document.getElementById('modal-result-text').textContent || '';
    // Use first H1 from markdown if present, else agent name + timestamp
    const m = text.match(/^#\\s+(.+)$/m);
    if (m) return m[1].replace(/[\\u{{1F300}}-\\u{{1FAFF}}\\u{{2600}}-\\u{{27BF}}]/gu, '').trim();
    return (currentAgent || 'report') + '_' + new Date().toISOString().slice(0,10);
  }}

  function downloadBlob(blob, filename) {{
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {{ URL.revokeObjectURL(url); a.remove(); }}, 500);
  }}

  function downloadResultTxt() {{
    const text = document.getElementById('modal-result-text').textContent || '';
    if (!text.trim()) return;
    const title = getResultExportTitle();
    const blob = new Blob([text], {{ type: 'text/plain;charset=utf-8' }});
    downloadBlob(blob, title + '.txt');
  }}

  async function downloadResultPdf() {{
    const text = document.getElementById('modal-result-text').textContent || '';
    if (!text.trim()) return;
    const btn = document.getElementById('btn-pdf');
    const orig = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Готовлю PDF…';
    try {{
      const r = await fetch(SERVER_BASE + '/api/export-pdf', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ content: text, title: getResultExportTitle() }})
      }});
      if (!r.ok) {{
        const err = await r.json().catch(() => ({{ error: 'HTTP ' + r.status }}));
        throw new Error(err.error || 'Server error');
      }}
      const blob = await r.blob();
      const cd = r.headers.get('content-disposition') || '';
      let filename = getResultExportTitle() + '.pdf';
      const m = cd.match(/filename\\*=UTF-8''([^;]+)/);
      if (m) {{ try {{ filename = decodeURIComponent(m[1]); }} catch (e) {{}} }}
      downloadBlob(blob, filename);
    }} catch (e) {{
      alert('Не удалось сгенерировать PDF: ' + e.message);
    }} finally {{
      btn.disabled = false;
      btn.textContent = orig;
    }}
  }}

  document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') closeModal();
  }});
</script>
</body>
</html>
"""


def render_agents(agents: list[dict]) -> str:
    if not agents:
        return '<div class="empty">Агенты не найдены</div>'
    return "\n".join(_agent_card(a) for a in agents)


def _agent_card(a: dict) -> str:
    tools = a["tools"]
    if not tools:
        tools_html = '<span class="tool all">All tools</span>'
    else:
        tools_html = "".join(f'<span class="tool">{html.escape(t)}</span>' for t in tools)
    search_text = " ".join([a["name"], a["description"], " ".join(tools), a["model"]]).lower()
    body_preview = (
        f'<details><summary>Системный промпт (превью)</summary>'
        f"<pre>{html.escape(a['body_preview'])}</pre></details>"
        if a["body_preview"]
        else ""
    )
    name_attr = html.escape(a["name"], quote=True).replace("'", "&#39;")
    display_attr = html.escape(a["display_name"], quote=True).replace("'", "&#39;")
    desc_attr = html.escape(a["description"], quote=True).replace("'", "&#39;")
    has_alias = a["display_name"] != a["name"]
    alias_row = (
        f'<div class="alias">{html.escape(a["name"])}</div>' if has_alias else ""
    )
    search_text_full = (search_text + " " + a["display_name"]).lower()
    return f"""
    <div class="card" data-search="{html.escape(search_text_full)}" data-source="{a['source']}">
      <h3>
        {html.escape(a['display_name'])}
        <span class="badge {a['source']}">{html.escape(a['source_label'])}</span>
        <span class="badge model">{html.escape(a['model'])}</span>
      </h3>
      {alias_row}
      <div class="desc">{html.escape(a['description']) or '<i>(нет описания)</i>'}</div>
      {f'<div class="desc-en">Оригинал: {html.escape(a["description_en"])}</div>' if a.get('description_en') else ''}
      <div class="tools">{tools_html}</div>
      <div class="actions">
        <button class="btn" data-needs-server onclick="openAgentModal('{name_attr}', '{display_attr}', '{desc_attr}')">▶ Запустить</button>
      </div>
      <div class="meta-row">{html.escape(a['path'])}</div>
      {body_preview}
    </div>
    """


def render_skills(skills: list[dict]) -> str:
    if not skills:
        return '<div class="empty">Скиллы не найдены</div>'
    return "\n".join(_skill_card(s) for s in skills)


def _skill_card(s: dict) -> str:
    search_text = " ".join([s["name"], s["description"]]).lower()
    body_preview = (
        f'<details><summary>Содержимое (превью)</summary>'
        f"<pre>{html.escape(s['body_preview'])}</pre></details>"
        if s["body_preview"]
        else ""
    )
    has_alias = s["display_name"] != s["name"]
    alias_row = (
        f'<div class="alias">{html.escape(s["name"])}</div>' if has_alias else ""
    )
    search_text_full = (search_text + " " + s["display_name"]).lower()
    return f"""
    <div class="card" data-search="{html.escape(search_text_full)}" data-source="{s['source']}">
      <h3>
        {html.escape(s['display_name'])}
        <span class="badge {s['source']}">{html.escape(s['source_label'])}</span>
      </h3>
      {alias_row}
      <div class="desc">{html.escape(s['description']) or '<i>(нет описания)</i>'}</div>
      <div class="meta-row">{html.escape(s['path'])}</div>
      {body_preview}
    </div>
    """


def render_plugins(agents: list[dict], skills: list[dict]) -> str:
    """Group plugin agents and skills by plugin name."""
    if not agents and not skills:
        return '<div class="empty">Плагины не установлены</div>'

    def plugin_of(item: dict) -> str:
        label = item.get("source_label", "")
        return label.split(":", 1)[1].strip() if ":" in label else label or "unknown"

    by_plugin: dict[str, dict[str, list[dict]]] = {}
    for a in agents:
        by_plugin.setdefault(plugin_of(a), {"agents": [], "skills": []})["agents"].append(a)
    for s in skills:
        by_plugin.setdefault(plugin_of(s), {"agents": [], "skills": []})["skills"].append(s)

    parts: list[str] = []
    for plugin_name in sorted(by_plugin.keys()):
        bucket = by_plugin[plugin_name]
        n_agents = len(bucket["agents"])
        n_skills = len(bucket["skills"])
        cards = "\n".join(_agent_card(a) for a in bucket["agents"]) + "\n" + "\n".join(
            _skill_card(s) for s in bucket["skills"]
        )
        parts.append(f"""
    <div class="plugin-section">
      <h2>{html.escape(plugin_name)}
        <span class="count">{n_agents} агент{'ов' if n_agents != 1 else ''} · {n_skills} скилл{'ов' if n_skills != 1 else ''}</span>
      </h2>
      <div class="grid">{cards}</div>
    </div>
    """)
    return "\n".join(parts)


def render_mcp(servers: list[dict], notes: list[str]) -> str:
    parts: list[str] = []
    for n in notes:
        parts.append(f'<div class="card"><div class="desc">{html.escape(n)}</div></div>')
    for s in servers:
        cfg_pretty = json.dumps(s.get("config", {}), indent=2, ensure_ascii=False)
        parts.append(
            f"""
        <div class="card" data-search="{html.escape(s['name'].lower())}" data-source="user">
          <h3>{html.escape(s['name'])}<span class="badge user">{html.escape(s['scope'])}</span></h3>
          <div class="meta-row">{html.escape(s['source'])}</div>
          <details><summary>Конфигурация</summary><pre>{html.escape(cfg_pretty)}</pre></details>
        </div>
        """
        )
    return "\n".join(parts) if parts else '<div class="empty">Ничего не найдено</div>'


def main() -> None:
    agents = collect_agents()
    skills = collect_skills()
    mcp_servers, mcp_notes = collect_mcp()
    enabled = collect_enabled_plugins()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        render_html(agents, skills, mcp_servers, mcp_notes, enabled), encoding="utf-8"
    )
    print(
        f"Wrote {OUTPUT} (agents={len(agents)}, skills={len(skills)}, "
        f"mcp={len(mcp_servers)}, enabled_plugins={len(enabled)})"
    )


if __name__ == "__main__":
    main()
