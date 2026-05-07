"""
Local launcher server for the Claude Code registry dashboard.

Run:
    python C:\\Users\\<you>\\.claude\\registry\\server.py

Then open http://127.0.0.1:8765/ in a browser.

Endpoints:
    GET  /                  -> dashboard.html (rebuilt on demand if missing)
    POST /api/run-agent     -> body {name, prompt} -> runs `claude -p --agent <name>`
    POST /api/rebuild       -> regenerates the dashboard
    GET  /api/health        -> {ok: true}
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
REGISTRY_DIR = HOME / ".claude" / "registry"
DASHBOARD = REGISTRY_DIR / "dashboard.html"
BUILDER = REGISTRY_DIR / "build_registry.py"
STATS_FILE = REGISTRY_DIR / "run_stats.json"
STATS_HISTORY_LIMIT = 10  # keep N latest durations per agent
_STATS_LOCK = threading.Lock()

HOST = "127.0.0.1"
PORT = 8765
RUN_TIMEOUT_SEC = 600


def find_claude() -> list[str]:
    """Return the argv prefix to invoke the claude CLI on this OS."""
    if sys.platform == "win32":
        for candidate in ("claude.cmd", "claude.exe", "claude"):
            p = shutil.which(candidate)
            if p:
                return [p]
        return ["cmd", "/c", "claude"]
    p = shutil.which("claude")
    return [p] if p else ["claude"]


CLAUDE_CMD = find_claude()

BROWSER_CANDIDATES = [
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for p in BROWSER_CANDIDATES:
        if Path(p).is_file():
            return p
    for name in ("msedge", "chrome", "chromium"):
        which = shutil.which(name)
        if which:
            return which
    return None


_INLINE_RE = re.compile(
    r"(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*\s][^*]*[^*\s]|\S)\*)"
)


def _render_inline(s: str) -> str:
    out_parts: list[str] = []
    last = 0
    for m in _INLINE_RE.finditer(s):
        out_parts.append(html_lib.escape(s[last : m.start()]))
        if m.group(2) is not None:
            out_parts.append(f"<strong>{html_lib.escape(m.group(2))}</strong>")
        elif m.group(3) is not None:
            out_parts.append(f"<code>{html_lib.escape(m.group(3))}</code>")
        elif m.group(4) is not None:
            out_parts.append(f"<em>{html_lib.escape(m.group(4))}</em>")
        last = m.end()
    out_parts.append(html_lib.escape(s[last:]))
    return "".join(out_parts)


def markdown_to_html(text: str) -> str:
    """Minimal markdown→HTML for the analyst's output: headers, lists, tables, bold/italic/code, blockquotes."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    in_list = None  # "ul" | "ol" | None

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            close_list()
            i += 1
            continue

        # Table: detect separator on next line "|---|---|"
        if stripped.startswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if re.match(r"^\|?\s*[:\-|]+\s*\|", sep):
                close_list()
                header_cells = [c.strip() for c in stripped.strip("|").split("|")]
                out.append('<table>')
                out.append(
                    "<thead><tr>"
                    + "".join(f"<th>{_render_inline(c)}</th>" for c in header_cells)
                    + "</tr></thead>"
                )
                out.append("<tbody>")
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith("|"):
                    cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    out.append(
                        "<tr>"
                        + "".join(f"<td>{_render_inline(c)}</td>" for c in cells)
                        + "</tr>"
                    )
                    j += 1
                out.append("</tbody></table>")
                i = j
                continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_render_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            close_list()
            content = stripped[1:].strip()
            out.append(f"<blockquote>{_render_inline(content)}</blockquote>")
            i += 1
            continue

        # Bullet list
        m = re.match(r"^([-*+])\s+(.+)$", stripped)
        if m:
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{_render_inline(m.group(2))}</li>")
            i += 1
            continue

        # Numbered list
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{_render_inline(m.group(2))}</li>")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        # Paragraph (collect consecutive non-blank lines)
        close_list()
        para_lines = [stripped]
        j = i + 1
        while j < len(lines) and lines[j].strip() and not re.match(
            r"^(#|>|[-*+]\s|\d+\.\s|\|)", lines[j].strip()
        ):
            para_lines.append(lines[j].strip())
            j += 1
        out.append(f"<p>{_render_inline(' '.join(para_lines))}</p>")
        i = j

    close_list()
    return "\n".join(out)


PDF_CSS = """
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; line-height: 1.5;
         color: #1a1a1a; font-size: 11pt; max-width: none; margin: 0; padding: 0; }
  h1 { font-size: 22pt; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 2px solid #ddd; }
  h2 { font-size: 15pt; margin: 22px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #eee; }
  h3 { font-size: 13pt; margin: 16px 0 6px; }
  h4 { font-size: 12pt; margin: 12px 0 4px; }
  p  { margin: 8px 0; }
  ul, ol { padding-left: 24px; margin: 6px 0; }
  li { margin: 3px 0; }
  table { border-collapse: collapse; margin: 10px 0; width: 100%; }
  th, td { border: 1px solid #cfd2d6; padding: 6px 9px; text-align: left; vertical-align: top; }
  th { background: #f4f5f7; font-weight: 600; }
  code { background: #f1f2f4; padding: 1px 5px; border-radius: 3px; font-family: Consolas, 'Courier New', monospace; font-size: 92%; }
  blockquote { border-left: 3px solid #cfd2d6; padding: 4px 12px; color: #555; margin: 8px 0; background: #fafbfc; }
  strong { font-weight: 600; }
  hr { border: none; border-top: 1px solid #ddd; margin: 14px 0; }
"""


def render_pdf(content: str, title: str) -> bytes:
    browser = find_browser()
    if not browser:
        raise RuntimeError("Browser (Edge/Chrome) not found for PDF rendering")
    body_html = markdown_to_html(content)
    page = (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{html_lib.escape(title)}</title>"
        f"<style>{PDF_CSS}</style></head><body>{body_html}</body></html>"
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="cc_export_"))
    try:
        html_path = tmpdir / "doc.html"
        pdf_path = tmpdir / "doc.pdf"
        html_path.write_text(page, encoding="utf-8")
        proc = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            capture_output=True,
            timeout=60,
        )
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError(
                f"PDF not produced (exit={proc.returncode}, stderr={proc.stderr[:200]!r})"
            )
        return pdf_path.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_stats() -> dict:
    if not STATS_FILE.is_file():
        return {}
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def record_run_stat(agent_name: str, duration_ms: int) -> None:
    with _STATS_LOCK:
        data = load_stats()
        bucket = data.setdefault(agent_name, [])
        bucket.append(int(duration_ms))
        del bucket[:-STATS_HISTORY_LIMIT]
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def stats_for(agent_name: str) -> dict:
    data = load_stats()
    durations = data.get(agent_name) or []
    if not durations:
        return {"durations": [], "median_ms": None, "count": 0}
    sorted_d = sorted(durations)
    mid = len(sorted_d) // 2
    median = (
        sorted_d[mid] if len(sorted_d) % 2 else (sorted_d[mid - 1] + sorted_d[mid]) // 2
    )
    return {"durations": durations, "median_ms": int(median), "count": len(durations)}


def safe_filename(s: str, default: str = "report") -> str:
    s = (s or default).strip()
    s = re.sub(r"[^\wЀ-ӿ .\-]", "_", s)
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:80] or default


def rebuild_dashboard() -> tuple[bool, str]:
    if not BUILDER.is_file():
        return False, f"Builder not found at {BUILDER}"
    try:
        proc = subprocess.run(
            [sys.executable, str(BUILDER)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        return proc.returncode == 0, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return False, "Rebuild timed out"


def run_agent(agent_name: str, user_prompt: str) -> dict:
    """Invoke `claude -p --agent <name> "<prompt>"` and return parsed result."""
    if not user_prompt.strip():
        return {"ok": False, "error": "Empty prompt"}

    args = CLAUDE_CMD + [
        "-p",
        user_prompt,
        "--agent",
        agent_name,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ]
    import time
    started = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=RUN_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timed out after {RUN_TIMEOUT_SEC}s"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"claude CLI not found: {e}"}
    duration_ms = int((time.monotonic() - started) * 1000)

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    parsed: dict | None = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"claude exited with code {proc.returncode}",
            "stdout": stdout,
            "stderr": stderr,
            "parsed": parsed,
        }

    result_text = ""
    if isinstance(parsed, dict):
        result_text = (
            parsed.get("result")
            or parsed.get("response")
            or parsed.get("text")
            or ""
        )
    if not result_text:
        result_text = stdout

    try:
        record_run_stat(agent_name, duration_ms)
    except OSError:
        pass

    return {
        "ok": True,
        "result": result_text,
        "raw": parsed if parsed is not None else stdout,
        "stderr": stderr if stderr else None,
        "duration_ms": duration_ms,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("[server] " + (format % args) + "\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        if not path.is_file():
            ok, log = rebuild_dashboard()
            if not ok:
                self._send_json(500, {"ok": False, "error": "Dashboard build failed", "log": log})
                return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html", "/dashboard.html"):
            self._send_html(DASHBOARD)
            return
        if self.path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        if self.path.startswith("/api/run-stats"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            agent_name = (qs.get("name") or [""])[0]
            if not agent_name:
                self._send_json(400, {"ok": False, "error": "Missing 'name'"})
                return
            self._send_json(200, {"ok": True, **stats_for(agent_name)})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        if self.path == "/api/run-agent":
            name = (body.get("name") or "").strip()
            prompt = body.get("prompt") or ""
            if not name:
                self._send_json(400, {"ok": False, "error": "Missing 'name'"})
                return
            result = run_agent(name, prompt)
            self._send_json(200 if result.get("ok") else 500, result)
            return

        if self.path == "/api/rebuild":
            ok, log = rebuild_dashboard()
            self._send_json(200 if ok else 500, {"ok": ok, "log": log})
            return

        if self.path == "/api/export-pdf":
            content = body.get("content") or ""
            title = body.get("title") or "Report"
            if not content.strip():
                self._send_json(400, {"ok": False, "error": "Empty content"})
                return
            try:
                pdf = render_pdf(content, title)
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
                return
            from urllib.parse import quote
            fname = safe_filename(title) + ".pdf"
            ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", fname) or "report.pdf"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(fname)}",
            )
            self.send_header("Content-Length", str(len(pdf)))
            self.end_headers()
            self.wfile.write(pdf)
            return

        if self.path == "/api/open-path":
            target = (body.get("path") or "").strip()
            if not target:
                self._send_json(400, {"ok": False, "error": "Missing 'path'"})
                return
            try:
                resolved = Path(target).expanduser()
            except (OSError, ValueError) as e:
                self._send_json(400, {"ok": False, "error": f"Bad path: {e}"})
                return
            if not resolved.exists():
                self._send_json(404, {"ok": False, "error": f"Path does not exist: {resolved}"})
                return
            try:
                if sys.platform == "win32":
                    if resolved.is_file():
                        subprocess.Popen(["explorer", "/select,", str(resolved)])
                    else:
                        os.startfile(str(resolved))  # noqa: S606
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(resolved)])
                else:
                    subprocess.Popen(["xdg-open", str(resolved)])
            except OSError as e:
                self._send_json(500, {"ok": False, "error": f"Could not open: {e}"})
                return
            self._send_json(200, {"ok": True, "opened": str(resolved)})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})


def main() -> None:
    if not DASHBOARD.is_file():
        print("Building dashboard for first time...")
        ok, log = rebuild_dashboard()
        if not ok:
            print("Build failed:", log)
            sys.exit(1)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Registry server running at http://{HOST}:{PORT}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
