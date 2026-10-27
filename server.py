#!/usr/bin/env python3
"""smartex server: a LaTeX workspace that people and AI agents share.

    python3 server.py --root DIR [--root DIR ...] [--port 8765] [--public HOST:PORT --allow-from ADDR ...]
                      [--projects-dir DIR] [--registry FILE]

People use the browser editor (index.html). Agents use the same JSON API, either directly, through the
`smartex` command-line client, or through the MCP server (`smartex_mcp.py`). GET /api/help describes
every endpoint. The server listens on 127.0.0.1; reach it from another computer through an SSH tunnel,
or through a reverse proxy (see "Behind a reverse proxy" in README.md). Every request must carry the
access token from ~/.smartex_token: header X-Token, ?token=, or the sign-in cookie that the sign-in page
sets. Requests should name their author in the X-Client header (for example "human:browser" or
"agent:claude-code"): every write is attributed, versioned and announced on the event feed.

New projects (the New project button, POST /api/projects, the create_project tool) are empty folders made
in --projects-dir (default ~/smartex-projects). With --registry FILE the server also serves every folder
listed in FILE (one per line) and adds to it each folder it serves, so created projects survive a restart.

--public HOST:PORT opens a second listener for a reverse proxy such as nginx. It accepts connections only
from this machine and from the --allow-from addresses or networks (at least one is required). The proxy
should send X-Forwarded-Proto and X-Forwarded-Prefix (the sub-path it serves smartex under), so that the
sign-in cookie is marked Secure and scoped to that path. With the cookie, every POST must also carry the
X-Client header and, when the browser sends one, a same-origin Origin header: another site cannot make
your browser change anything.

The built-in assistant (the Assistant tab, /api/agent) is one long-running Claude Code process per
project, started in headless streaming mode with your own Claude Code login; it stays awake between
messages and resumes its saved conversation after a restart. It is meant for the owner of the account
the server runs under; see README.md.

Standard library only. Optional extras: pdftotext and pdftoppm (poppler) enable PDF text and page images.
"""
import argparse
import gzip
import hashlib
import html
import http.cookies
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

VERSION = '0.5.0'
EDITABLE = {'.tex', '.bib', '.sty', '.cls', '.bst', '.md', '.txt', '.cfg'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.smartex'}
HERE = Path(__file__).resolve().parent
COOKIE = 'smartex_token'
PROJECT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
REGISTRY_HEADER = ('# Projects that smartex serves, one folder per line (smartex.sh start adds to it; so does creating a\n'
                   '# project). Delete a line to stop serving that project; the folder itself is kept.\n')
SIGNIN_PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in to smartex</title>
<style>
  :root { color-scheme: dark; --bg: #1d2a27; --panel: #243430; --ink: #e9efe9; --mute: #9fb3ab; --accent: #f0c94a; --bad: #ff8a7a; }
  @media (prefers-color-scheme: light) { :root { color-scheme: light; --bg: #f6f4ee; --panel: #ffffff; --ink: #1f2a27; --mute: #5d6d67; --accent: #b8860b; --bad: #b3261e; } }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 16px; }
  form { width: 100%; max-width: 380px; background: var(--panel); padding: 28px; border-radius: 10px; }
  h1 { margin: 0 0 6px; font-size: 22px; }
  p { margin: 0 0 18px; color: var(--mute); font-size: 14px; }
  label { display: block; font-size: 14px; margin-bottom: 6px; }
  input[type=password] { width: 100%; padding: 10px 12px; font: inherit; border: 1px solid var(--mute); border-radius: 6px;
                         background: transparent; color: var(--ink); }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button { margin-top: 16px; width: 100%; padding: 10px; font: inherit; font-weight: 700; border: 0; border-radius: 6px;
           background: var(--accent); color: #1d2a27; cursor: pointer; }
  .err { color: var(--bad); margin: 12px 0 0; font-size: 14px; }
</style></head>
<body><form method="post" action="login">
  <h1>smartex</h1>
  <p>Enter the access token. It is in ~/.smartex_token on the server.</p>
  <input type="text" name="username" value="smartex" autocomplete="username" hidden>
  <label for="token">Access token</label>
  <input id="token" name="token" type="password" autocomplete="current-password" autofocus required>
  __ERROR__
  <button type="submit">Sign in</button>
</form></body></html>
'''
# directories searched for latexmk before PATH: $SMARTEX_TEXBIN, a user-space TeX Live, MacTeX
TEXBINS = [Path(p) for p in (os.environ.get('SMARTEX_TEXBIN', ''),
                             str(Path.home() / 'texlive' / '2026' / 'bin' / 'x86_64-linux'),
                             '/Library/TeX/texbin') if p]
ERROR_RE = re.compile(r'^(?P<file>[^:\n]+\.(?:tex|sty|cls|bib|bbl)):(?P<line>\d+): (?P<msg>.+)$', re.M)
WARN_RE = re.compile(r'^((?:LaTeX|Package \S+|Class \S+) Warning: .+?)(?:\n\n|\Z)', re.M | re.S)
LINE_RE = re.compile(r'on input line (\d+)')
OVERFULL_RE = re.compile(r'^(Overfull \\hbox \(([\d.]+)pt too wide\) (?:in paragraph|detected) at lines? (\d+)(?:--\d+)?)', re.M)
SYNC_REC = re.compile(r'^[\[\(hkgx$v](\d+),(\d+):(-?\d+),(-?\d+)')
SP_TO_BP = 72.0 / 72.27 / 65536.0


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


# ---------------------------------------------------------------------------------------------------
# event feed: every change is announced here; agents can block on it (long poll)
# ---------------------------------------------------------------------------------------------------
class Events:
    def __init__(self):
        self.items, self.seq, self.cond = [], 0, threading.Condition()

    def emit(self, project, kind, **data):
        with self.cond:
            self.seq += 1
            self.items.append(dict(seq=self.seq, time=time.strftime('%Y-%m-%dT%H:%M:%S'), project=project, type=kind, **data))
            del self.items[:-2000]
            self.cond.notify_all()

    def since(self, project, seq, timeout):
        deadline = time.time() + max(0.0, min(timeout, 60.0))
        with self.cond:
            while True:
                out = [e for e in self.items if e['seq'] > seq and e['project'] == project]
                if out or time.time() >= deadline:
                    return out, self.seq
                self.cond.wait(deadline - time.time())


EVENTS = Events()


# ---------------------------------------------------------------------------------------------------
# a project folder
# ---------------------------------------------------------------------------------------------------
class Project:
    def __init__(self, root):
        self.root = root.resolve()
        self.name = self.root.name
        self.lock = threading.RLock()
        self.compile_lock = threading.Lock()
        self.meta = self.root / '.smartex'
        self.last_compile = None
        self.presence = {}

    # ---- paths ----
    def path(self, rel, must_exist=True):
        if not rel:
            raise ApiError(400, 'missing path')
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise ApiError(403, 'path is outside the project')
        if self.meta in target.parents or target == self.meta:
            raise ApiError(403, 'the .smartex folder is managed by smartex')
        if must_exist and not target.is_file():
            raise ApiError(404, f'no such file: {rel}')
        return target

    def rel(self, p):
        return str(Path(p).resolve().relative_to(self.root))

    def editable(self, target):
        if target.suffix not in EDITABLE:
            raise ApiError(403, f'{target.suffix or "this"} files cannot be edited; allowed: {", ".join(sorted(EDITABLE))}')

    def files(self):
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.'))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if p.suffix in EDITABLE:
                    st = p.stat()
                    out.append(dict(path=str(p.relative_to(self.root)), size=st.st_size, mtime=int(st.st_mtime),
                                    sha=digest(p.read_bytes())))
        return out

    def main_file(self):
        tex = [f['path'] for f in self.files() if f['path'].endswith('.tex')]
        for f in sorted(tex, key=lambda f: (f != 'main.tex', f)):
            try:
                if '\\documentclass' in (self.root / f).read_text(errors='replace')[:20000]:
                    return f
            except OSError:
                pass
        return tex[0] if tex else None

    def main_path(self, main=None):
        main = main or self.main_file()
        if not main:
            raise ApiError(404, 'this project has no .tex file')
        return self.path(main)

    # ---- versioned writes ----
    def _history_dir(self, rel):
        return self.meta / 'history' / rel.replace('/', '%2F')

    def _snapshot(self, target, client):
        if target.exists():
            d = self._history_dir(self.rel(target))
            d.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y%m%dT%H%M%S') + f'{time.time() % 1:.3f}'[1:]
            safe_client = re.sub(r'[^A-Za-z0-9_.:-]', '_', client)[:40]
            shutil.copy2(target, d / f'{stamp}__{safe_client}{target.suffix}')

    def write(self, rel, data, client, base_sha=None, force=False, must_not_exist=False):
        target = self.path(rel, must_exist=False)
        self.editable(target)
        with self.lock:
            existed = target.exists()
            if must_not_exist and existed:
                raise ApiError(409, f'{rel} already exists')
            if existed and not force and base_sha:
                current = digest(target.read_bytes())
                if current != base_sha:
                    raise ApiError(409, f'{rel} changed since you read it', sha=current)
            self._snapshot(target, client)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f'.{target.name}.smartex-tmp')
            tmp.write_bytes(data)
            os.replace(tmp, target)
            sha = digest(data)
        EVENTS.emit(self.name, 'file_written' if existed else 'file_created', path=rel, sha=sha, client=client)
        return sha

    def edit(self, rel, edits, client, base_sha=None):
        target = self.path(rel)
        self.editable(target)
        with self.lock:
            text = target.read_text()
            if base_sha and digest(text.encode()) != base_sha:
                raise ApiError(409, f'{rel} changed since you read it', sha=digest(text.encode()))
            for i, e in enumerate(edits):
                old, new = e.get('old'), e.get('new', '')
                if not old:
                    raise ApiError(400, f'edit {i}: "old" must be a non-empty string')
                n = text.count(old)
                if n != 1:
                    raise ApiError(422, f'edit {i}: "old" occurs {n} times in {rel}; it must occur exactly once',
                                   edit=i, occurrences=n)
                text = text.replace(old, new)
            return self.write(rel, text.encode(), client, force=True), len(edits)

    def history(self, rel):
        d = self._history_dir(rel)
        out = []
        for p in sorted(d.glob('*'), reverse=True) if d.exists() else []:
            stamp, _, client = p.stem.partition('__')
            # each snapshot is the content as it was just before `client` overwrote it
            out.append(dict(id=p.name, time=stamp, overwritten_by=client, size=p.stat().st_size, sha=digest(p.read_bytes())))
        return out

    # ---- build ----
    def compile(self, main, client):
        main_path = self.main_path(main)
        env = dict(os.environ)
        env['PATH'] = ':'.join([str(b) for b in TEXBINS if b.is_dir()] + [env.get('PATH', '')])
        cmd = ['latexmk', '-pdf', '-interaction=nonstopmode', '-file-line-error', '-synctex=1', main_path.name]
        # latexmk skips the run when the PDF looks up to date, so a PDF made elsewhere (or without SyncTeX)
        # would never get the SyncTeX data that cursor-following needs: force a run in that case
        pdf_now, sync_now = main_path.with_suffix('.pdf'), main_path.with_suffix('.synctex.gz')
        if pdf_now.exists() and (not sync_now.exists() or sync_now.stat().st_mtime < pdf_now.stat().st_mtime - 2
                                 or not synctex_matches_folder(sync_now, main_path.parent)):
            cmd.insert(1, '-g')
        EVENTS.emit(self.name, 'compile_started', main=self.rel(main_path), client=client)
        t0 = time.time()
        with self.compile_lock:
            try:
                code = subprocess.run(cmd, cwd=main_path.parent, env=env, capture_output=True, text=True, timeout=240).returncode
            except subprocess.TimeoutExpired:
                code = -1
            except FileNotFoundError:
                code = -2
        log_path = main_path.with_suffix('.log')
        log = log_path.read_text(errors='replace') if log_path.exists() else ''
        base = os.path.relpath(main_path.parent, self.root)
        errors = [dict(file=os.path.normpath(os.path.join(base, m.group('file'))), line=int(m.group('line')),
                       msg=m.group('msg').strip()) for m in ERROR_RE.finditer(log)]
        warnings = []
        for m in WARN_RE.finditer(log):
            text = ' '.join(m.group(1).split())
            ln = LINE_RE.search(text)
            warnings.append(dict(line=int(ln.group(1)) if ln else None, msg=text))
        overfull = [dict(line=int(m.group(3)), points=float(m.group(2)), msg=m.group(1)) for m in OVERFULL_RE.finditer(log)]
        if code == -1:
            errors.insert(0, dict(file=self.rel(main_path), line=0, msg='compilation took longer than 240 s and was stopped'))
        if code == -2:
            errors.insert(0, dict(file=self.rel(main_path), line=0, msg='latexmk was not found on this machine'))
        pdf = main_path.with_suffix('.pdf')
        result = dict(ok=(code == 0 and not errors), main=self.rel(main_path), seconds=round(time.time() - t0, 1),
                      errors=errors[:100], warnings=warnings[:100], overfull=overfull[:100], pdf=pdf.exists(),
                      pages=pdf_pages(pdf) if pdf.exists() else 0, client=client,
                      finished=time.strftime('%Y-%m-%dT%H:%M:%S'))
        self.last_compile = result
        EVENTS.emit(self.name, 'compile_finished', ok=result['ok'], errors=len(errors), warnings=len(warnings) + len(overfull),
                    pages=result['pages'], client=client)
        return result

    # ---- notes (review comments anchored to lines) ----
    def notes(self):
        f = self.meta / 'notes.json'
        return json.loads(f.read_text()) if f.exists() else []

    def save_notes(self, notes):
        self.meta.mkdir(exist_ok=True)
        (self.meta / 'notes.json').write_text(json.dumps(notes, indent=1))


# ---------------------------------------------------------------------------------------------------
# PDF helpers (poppler) and a small SyncTeX reader
# ---------------------------------------------------------------------------------------------------
def pdf_pages(pdf):
    if shutil.which('pdfinfo'):
        out = subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True).stdout
        m = re.search(r'^Pages:\s+(\d+)', out, re.M)
        if m:
            return int(m.group(1))
    return len(re.findall(rb'/Type\s*/Page[^s]', pdf.read_bytes()))


def pdf_page_sizes(pdf):
    """Page sizes in PostScript points, [(width, height), ...]; empty if pdfinfo is missing."""
    if not shutil.which('pdfinfo'):
        return []
    n = pdf_pages(pdf)
    out = subprocess.run(['pdfinfo', '-f', '1', '-l', str(n), str(pdf)], capture_output=True, text=True).stdout
    return [(float(w), float(h)) for w, h in re.findall(r'^Page\s+\d+\s+size:\s+([\d.]+) x ([\d.]+)', out, re.M)]


_PAGE_CACHE, _SYNC_CACHE = {}, {}


def synctex_matches_folder(sync, folder):
    """False when the SyncTeX data was written for files in another folder (a moved or copied project)."""
    try:
        with gzip.open(sync, 'rt', errors='replace') as f:
            for _ in range(50):
                line = f.readline()
                if line.startswith('Input:'):
                    path = line.rstrip('\n').split(':', 2)[2]
                    if not os.path.isabs(path):
                        return True
                    p = Path(os.path.normpath(path))
                    return p.parent == folder.resolve() or folder.resolve() in p.parents
    except (OSError, EOFError):
        return False
    return True


def synctex_records(project, main_path):
    f = main_path.with_suffix('.synctex.gz')
    if not f.exists():
        raise ApiError(404, 'no SyncTeX data yet: compile first')
    key = (str(f), f.stat().st_mtime_ns)
    if key not in _SYNC_CACHE:
        _SYNC_CACHE.clear()
        _SYNC_CACHE[key] = _parse_synctex(project, main_path, f)
    return _SYNC_CACHE[key]


def _parse_synctex(project, main_path, f):
    inputs, records, page, unit, xo, yo = {}, [], 0, 1.0, 0, 0
    for line in gzip.open(f, 'rt', errors='replace'):
        c = line[:1]
        if line.startswith('Input:'):
            tag, _, path = line[6:].rstrip('\n').partition(':')
            p = Path(path) if os.path.isabs(path) else (main_path.parent / path)
            try:
                inputs[int(tag)] = project.rel(p)
            except ValueError:
                inputs[int(tag)] = path
        elif line.startswith('Unit:'):
            unit = float(line[5:])
        elif line.startswith('X Offset:'):
            xo = int(line[9:])
        elif line.startswith('Y Offset:'):
            yo = int(line[9:])
        elif c == '{':
            page = int(line[1:])
        elif page:
            m = SYNC_REC.match(line)
            if m:
                tag, ln, h, v = map(int, m.groups())
                records.append((page, inputs.get(tag), ln, (h * unit + xo) * SP_TO_BP, (v * unit + yo) * SP_TO_BP))
    return records


# ---------------------------------------------------------------------------------------------------
# the assistant: one persistent headless Claude Code process per project
# ---------------------------------------------------------------------------------------------------
AGENT_PROMPT = """You are the assistant inside smartex, a LaTeX workspace shared with the person who owns it.
The current directory is the project folder. Rules:
- Make every change through the smartex MCP tools (edit_file with base_sha from read_file, write_file,
  create_file, ...). They attribute and version your changes and show them live in the person's editor.
  The built-in Edit and Write tools are disabled for this reason.
- Check your work with compile, and look at the result with view_pdf_page or pdf_text.
- Text the person wrote is theirs. Unless they explicitly ask you to change it, comment with add_note on
  the relevant line instead of editing. Use open_in_editor to point them to a location.
- Each message may start with "[smartex context: ...]" saying which file, line and selection the person is
  looking at. Use it to resolve "this", "here", "the selected sentence".
- If the person asks for a new project, make it with create_project, then work in it by passing its name as
  "project" to the other tools (start with create_file for its main .tex file). Your current directory
  stays this project's folder.
- For the literature, the frontier CLI is available through Bash if installed: fr search, fr abs, fr toc,
  fr read, fr eq, fr bib.
- Replies are shown in a narrow side panel: keep them short and concrete."""


class Agent:
    def __init__(self, project, port):
        self.p, self.port = project, port
        self.dir = project.meta / 'agent'
        self.proc, self.status, self.busy, self.error = None, 'stopped', False, None
        self.lock = threading.RLock()
        self.entries = []
        self.stderr_tail = []
        state = self._load_state()
        # requested settings ('' = Claude Code's own default) and the model Claude Code reports it is running
        self.session_id = state.get('session_id')
        self.model_req, self.effort = state.get('model_req', ''), state.get('effort', '')
        self.model = None
        tf = self.dir / 'transcript.jsonl'
        if tf.exists():
            self.entries = [json.loads(l) for l in tf.read_text().splitlines() if l.strip()][-500:]

    # ---- persistence ----
    def _load_state(self):
        f = self.dir / 'state.json'
        return json.loads(f.read_text()) if f.exists() else {}

    def _save_state(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / 'state.json').write_text(json.dumps(dict(session_id=self.session_id, model_req=self.model_req,
                                                             effort=self.effort)))

    def _add(self, **entry):
        with self.lock:
            entry.update(index=(self.entries[-1]['index'] + 1) if self.entries else 0, time=time.strftime('%Y-%m-%dT%H:%M:%S'))
            self.entries.append(entry)
            del self.entries[:-500]
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / 'transcript.jsonl', 'a') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        EVENTS.emit(self.p.name, 'agent_output', index=entry['index'], role=entry['role'], client='agent:claude')

    def _set(self, status=None, busy=None, error=None):
        if status is not None:
            self.status = status
        if busy is not None:
            self.busy = busy
        if error is not None:
            self.error = error
        EVENTS.emit(self.p.name, 'agent_status', status=self.status, busy=self.busy, client='agent:claude')

    def info(self):
        return dict(status=self.status, busy=self.busy, session_id=self.session_id, model=self.model,
                    model_requested=self.model_req, effort=self.effort, efforts=list(EFFORTS),
                    pid=self.proc.pid if self.running() else None, error=self.error, entries=len(self.entries),
                    claude=claude_binary() or None)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    # ---- process ----
    def configure(self, model=None, effort=None):
        """Choose the model and effort; a running assistant restarts with them and resumes the conversation."""
        if model is not None and not re.fullmatch(r'[A-Za-z0-9._\[\]-]{0,64}', model):
            raise ApiError(400, f'invalid model name {model!r}')
        if effort is not None and effort not in ('',) + EFFORTS:
            raise ApiError(400, f'effort must be one of {", ".join(EFFORTS)} (or empty for the default)')
        with self.lock:
            if self.busy:
                raise ApiError(409, 'the assistant is working; wait until it finishes or press Stop, then change settings')
            changed = (model is not None and model != self.model_req) or (effort is not None and effort != self.effort)
            if model is not None:
                self.model_req = model
            if effort is not None:
                self.effort = effort
            self._save_state()
            if changed and self.running():
                self.stop()
                self.start()

    def start(self, model=None):
        with self.lock:
            if self.running():
                return
            exe = claude_binary()
            if not exe:
                raise ApiError(501, 'smartex cannot find Claude Code (the claude command) on this server. Install it, or '
                                    'restart smartex with SMARTEX_CLAUDE set to its full path.')
            if model:
                self.model_req = model
            self.dir.mkdir(parents=True, exist_ok=True)
            mcp = self.dir / 'mcp.json'
            mcp.write_text(json.dumps({'mcpServers': {'smartex': {
                'command': sys.executable,
                'args': [str(HERE / 'smartex_mcp.py'), '--url', f'http://127.0.0.1:{self.port}',
                         '--client', 'agent:claude', '--project', self.p.name]}}}, indent=1))
            cmd = [exe, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
                   '--permission-mode', 'dontAsk', '--strict-mcp-config', '--mcp-config', str(mcp),
                   '--append-system-prompt', AGENT_PROMPT,
                   '--disallowedTools', 'Edit', 'Write', 'NotebookEdit',
                   '--allowedTools', 'mcp__smartex', 'Bash(fr *)', 'Bash(fr)', 'Read', 'Grep', 'Glob']
            if self.model_req:
                cmd += ['--model', self.model_req]
            if self.effort:
                cmd += ['--effort', self.effort]
            if self.session_id:
                cmd += ['--resume', self.session_id]
            # the owner's Claude Code login by default; an explicit SMARTEX_AGENT_API_KEY (for shared setups) overrides it
            env = {k: v for k, v in os.environ.items() if k != 'ANTHROPIC_API_KEY'}
            env['PATH'] = agent_path(exe)
            if os.environ.get('SMARTEX_AGENT_API_KEY'):
                env['ANTHROPIC_API_KEY'] = os.environ['SMARTEX_AGENT_API_KEY']
            self.proc = subprocess.Popen(cmd, cwd=self.p.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
            self.error, self.stderr_tail = None, []
            self._set(status='starting', busy=False)
            threading.Thread(target=self._read_stdout, args=(self.proc,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()

    def stop(self):
        with self.lock:
            if self.running():
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
            self._set(status='stopped', busy=False)

    def reset(self):
        self.stop()
        with self.lock:
            tf = self.dir / 'transcript.jsonl'
            if tf.exists():
                tf.rename(self.dir / f'transcript-{time.strftime("%Y%m%dT%H%M%S")}.jsonl')
            self.entries, self.session_id = [], None
            self._save_state()
        self._set(status='stopped', busy=False)

    def send(self, text, client, context=None):
        if not text.strip():
            raise ApiError(400, 'empty message')
        self.start()
        prompt = (f'[smartex context: {context}]\n\n' if context else '') + text
        self._add(role='user', text=text, client=client, context=context)
        with self.lock:
            self.proc.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt}}) + '\n')
            self.proc.stdin.flush()
        self._set(busy=True)

    def _read_stdout(self, proc):
        for line in proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            t = m.get('type')
            if t == 'system' and m.get('subtype') == 'init':
                self.session_id = m.get('session_id') or self.session_id
                self.model = m.get('model') or self.model
                self._save_state()
                self._set(status='ready')
            elif t == 'assistant':
                for block in m.get('message', {}).get('content', []):
                    kind = block.get('type')
                    if kind == 'text' and block.get('text', '').strip():
                        self._add(role='assistant', text=block['text'])
                    elif kind == 'thinking' and block.get('thinking', '').strip():
                        self._add(role='thinking', text=block['thinking'][:6000])
                    elif kind == 'tool_use':
                        name = block.get('name', '').replace('mcp__smartex__', '')
                        args = block.get('input') or {}
                        brief = {k: (v if len(str(v)) < 120 else str(v)[:117] + '...') for k, v in args.items()
                                 if k in ('path', 'line', 'query', 'page', 'main', 'command', 'id', 'to', 'text', 'pattern',
                                          'file_path', 'description')}
                        detail = json.dumps(args, ensure_ascii=False, indent=1)
                        self._add(role='tool', name=name, input=brief, id=block.get('id'),
                                  detail=detail if len(detail) < 3000 else detail[:2990] + ' ...')
            elif t == 'user':
                for block in (m.get('message', {}).get('content') or []):
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        self._add(role='tool_result', id=block.get('tool_use_id'), ok=not block.get('is_error'),
                                  text=tool_result_text(block.get('content'))[:2500])
            elif t == 'result':
                self._add(role='result', ok=not m.get('is_error'), turns=m.get('num_turns'),
                          seconds=round((m.get('duration_ms') or 0) / 1000, 1), subtype=m.get('subtype'))
                self._set(busy=False)
        code = proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None
                err = None if code in (0, -15) else f'Claude Code exited with code {code}: ' + ' | '.join(self.stderr_tail[-3:])
                self._set(status='stopped', busy=False, error=err)

    def _read_stderr(self, proc):
        for line in proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.strip()])[-20:]


EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def tool_result_text(content):
    """Readable text of a tool result (a string, or a list of text and image blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            parts.append(c.get('text', '') if c.get('type') == 'text' else f'[{c.get("type", "item")}]')
    return '\n'.join(p for p in parts if p)


def claude_binary():
    """SMARTEX_CLAUDE, else `claude` on PATH, else the usual install places. smartex may be started from a shell
    with a bare PATH (a cron job, another agent's sandbox), so PATH alone is not enough."""
    found = os.environ.get('SMARTEX_CLAUDE') or shutil.which('claude')
    if found:
        return found
    home = Path.home()
    nvm = sorted((home / '.nvm' / 'versions' / 'node').glob('v*/bin/claude'),
                 key=lambda f: [int(x) if x.isdigit() else 0 for x in f.parts[-3].lstrip('v').split('.')], reverse=True)
    for f in [home / '.local/bin/claude', home / '.claude/local/claude', *nvm, home / '.npm-global/bin/claude',
              home / '.bun/bin/claude', Path('/usr/local/bin/claude'), Path('/opt/homebrew/bin/claude')]:
        if f.is_file() and os.access(f, os.X_OK):
            return str(f)
    return None


def agent_path(exe):
    """PATH for the assistant: the folder of its claude, ~/.local/bin (where `fr` usually is), then smartex's own."""
    dirs = [str(Path(exe).parent), str(Path.home() / '.local/bin')] + os.environ.get('PATH', '').split(os.pathsep)
    return os.pathsep.join(dict.fromkeys(d for d in dirs if d))


AGENTS = {}


def agent_for(project, port):
    if project.name not in AGENTS:
        AGENTS[project.name] = Agent(project, port)
    return AGENTS[project.name]


# ---------------------------------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------------------------------
ENDPOINTS = [
    # method, path, parameters, description
    ('GET', '/api/help', '', 'This catalogue.'),
    ('GET', '/api/projects', '', 'Projects served, with their main file.'),
    ('POST', '/api/projects', 'body {name}', 'Create a new, empty project: a folder NAME (letters, digits, ".", "_", "-") in the projects folder (projects_dir in this catalogue). Every project\'s feed announces it as project_created.'),
    ('GET', '/api/files', 'project', 'Editable files with size, mtime and sha; the main file; whether a PDF exists.'),
    ('GET', '/api/file', 'project, path, [start, end (1-based lines)], [numbered=1]', 'Read a file (or a line range). Returns content, sha, total lines.'),
    ('POST', '/api/file', 'project, path; body {content, base_sha?, force?}', 'Create or overwrite a file. With base_sha, refuses (409) if the file changed since you read it.'),
    ('POST', '/api/edit', 'project, path; body {edits: [{old, new}], base_sha?}', 'Exact find-and-replace; every "old" must occur exactly once (else 422). All edits apply atomically.'),
    ('POST', '/api/new', 'project, path; body {content?}', 'Create a new file (409 if it exists).'),
    ('POST', '/api/rename', 'project, path, to', 'Rename a file (versioned).'),
    ('POST', '/api/delete', 'project, path', 'Delete a file; its last content stays in history and can be restored.'),
    ('GET', '/api/search', 'project, q, [regex=1], [context=N], [path=FILE]', 'Search all editable files; returns path, line, text and context lines.'),
    ('POST', '/api/compile', 'project; body {main?}', 'Run latexmk. Returns ok, errors (file, line, msg), warnings, overfull boxes, pages.'),
    ('GET', '/api/compile/last', 'project', 'The most recent compile result (from anyone).'),
    ('GET', '/api/log', 'project, [main]', 'Full LaTeX log of the main file.'),
    ('GET', '/api/pdf', 'project, [main]', 'The compiled PDF (application/pdf).'),
    ('GET', '/api/pdf/info', 'project, [main]', 'Page count, page sizes in points, file size and modification time of the PDF.'),
    ('GET', '/api/pdf/text', 'project, [page], [main]', 'Text of the PDF or one page (needs pdftotext).'),
    ('GET', '/api/pdf/page', 'project, page, [dpi=110], [main]', 'One page rendered as PNG, for agents that can see images (needs pdftoppm).'),
    ('GET', '/api/synctex/view', 'project, path, line', 'Where a source line appears in the PDF: page and position in points from the top left (approximate).'),
    ('GET', '/api/synctex/edit', 'project, page, x, y', 'Which source line produced a point in the PDF (approximate).'),
    ('GET', '/api/history', 'project, path', 'Earlier versions of a file, newest first (a copy is kept before every write).'),
    ('GET', '/api/history/get', 'project, path, id', 'Content of one earlier version.'),
    ('POST', '/api/history/restore', 'project, path, id', 'Restore an earlier version (the current one is versioned first).'),
    ('GET', '/api/presence', 'project', 'What each client is looking at: file, cursor line, selection.'),
    ('POST', '/api/presence', 'project; body {path, line, ch?, selection?}', 'Report your own position (the browser editor does this).'),
    ('POST', '/api/ui/open', 'project; body {path, line?, message?}', 'Ask the people using the browser editor to open a file at a line, with an optional message.'),
    ('GET', '/api/notes', 'project, [path], [all=1]', 'Review notes anchored to lines (open ones unless all=1).'),
    ('POST', '/api/notes', 'project; body {path, line, text}', 'Add a review note: comment on text without editing it.'),
    ('POST', '/api/notes/resolve', 'project; body {id}', 'Mark a note as resolved.'),
    ('GET', '/api/agent', 'project', 'State of the assistant: stopped, starting, ready; busy; session id; model.'),
    ('POST', '/api/agent/message', 'project; body {text, context?: bool (default true)}', 'Send a message to the assistant (starts or resumes it). The person\'s current file, line and selection are added unless context is false.'),
    ('GET', '/api/agent/transcript', 'project, [since=INDEX]', 'The conversation with the assistant: user, assistant, thinking, tool (with id and full input), tool_result (matching id, ok, text) and result entries.'),
    ('POST', '/api/agent/start', 'project; body {model?}', 'Start (or resume) the assistant process without sending a message.'),
    ('POST', '/api/agent/config', 'project; body {model?, effort?}', 'Choose the model (alias such as opus, sonnet, haiku, fable, or a full name; empty for the default) and effort (low, medium, high, xhigh, max; empty for the default). A running assistant restarts and resumes the conversation.'),
    ('POST', '/api/agent/stop', 'project', 'Stop the assistant process; the conversation is kept and resumes with the next message.'),
    ('POST', '/api/agent/reset', 'project', 'Stop the assistant and start a new conversation next time (the old transcript is archived).'),
    ('GET', '/api/events', 'project, [since=SEQ], [timeout=S <= 60]', 'Change feed (file_written, file_created, file_renamed, file_deleted, compile_started, compile_finished, note_added, note_resolved, ui_open, agent_status, agent_output, project_created). Blocks until something happens or timeout.'),
]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f'smartex/{VERSION}'
    projects = {}
    token = ''
    port = 8765
    allow_from = []          # networks the public listener accepts, besides the loopback
    projects_dir = Path.home() / 'smartex-projects'     # where new projects are made
    registry = None          # file listing the project folders to serve on every start, if any

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s %s\n' % (time.strftime('%H:%M:%S'), self.headers.get('X-Client', '-') if self.headers else '-', fmt % args))
        sys.stderr.flush()       # Python 3.8 buffers a redirected stderr; keep the log current

    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for k, v in getattr(self, '_extra_headers', []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    @property
    def client(self):
        return (self.headers.get('X-Client') or 'unknown')[:60]

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            raise ApiError(400, 'body must be JSON')

    def arg(self, q, name, default=None, required=False):
        v = q.get(name, [None])[0]
        if v is None:
            if required:
                raise ApiError(400, f'missing parameter "{name}"')
            return default
        return v

    def project(self, q):
        name = self.arg(q, 'project')
        if name is None and len(self.projects) == 1:
            return next(iter(self.projects.values()))
        if name not in self.projects:
            raise ApiError(404, f'unknown project {name!r}; known: {", ".join(self.projects)}')
        return self.projects[name]

    # ---- who may connect, and how they prove it ----
    def peer_allowed(self):
        """The loopback listener takes everyone on this machine; the public one only the proxy's addresses."""
        if not getattr(self.server, 'public', False):
            return True
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        ip = getattr(ip, 'ipv4_mapped', None) or ip
        return ip.is_loopback or any(ip in net for net in self.allow_from)

    def cookie_token(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie') or '')
        except http.cookies.CookieError:
            return ''
        return jar[COOKIE].value if COOKIE in jar else ''

    def authenticate(self, q):
        """(ok, by_cookie). A header or ?token= is shown deliberately; the cookie is sent by the browser on its own."""
        for given, by_cookie in ((self.headers.get('X-Token') or '', False), (self.arg(q, 'token', '') or '', False),
                                 (self.cookie_token(), True)):
            if given:
                return secrets.compare_digest(given.encode(), self.token.encode()), by_cookie
        return False, False

    def same_origin(self):
        origin = self.headers.get('Origin')
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc == (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '')

    def prefix(self):
        p = self.headers.get('X-Forwarded-Prefix') or '/'
        if not re.fullmatch(r'/[A-Za-z0-9_./-]*', p) or '..' in p:
            return '/'
        return p.rstrip('/') + '/'

    def send_signin(self, code=200, error=''):
        msg = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ''
        self.send(code, SIGNIN_PAGE.replace('__ERROR__', msg).encode(), 'text/html; charset=utf-8')

    def post_signin(self):
        n = int(self.headers.get('Content-Length') or 0)
        form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode('utf-8', 'replace')) if n else {}
        given = (form.get('token') or [''])[0].strip()
        if not given or not secrets.compare_digest(given.encode(), self.token.encode()):
            time.sleep(1)
            return self.send_signin(403, 'That token is not right.')
        secure = '; Secure' if (self.headers.get('X-Forwarded-Proto') or '').lower() == 'https' else ''
        self._extra_headers = [('Set-Cookie', f'{COOKIE}={self.token}; Path={self.prefix()}; Max-Age=2592000; '
                                              f'HttpOnly; SameSite=Strict{secure}'), ('Location', './')]
        self.send(303, b'', 'text/plain')

    def dispatch(self, method):
        self._extra_headers = []
        if not self.peer_allowed():
            return self.send(403, {'error': 'connections from this address are not accepted'})
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == '/login':
            return self.post_signin() if method == 'post' else self.send_signin()
        ok, by_cookie = self.authenticate(q)
        if not ok:
            if method == 'get' and url.path in ('/', '/index.html'):
                return self.send_signin()
            return self.send(403, {'error': 'missing or wrong access token (header X-Token, ?token=, or sign in at /login); it is in ~/.smartex_token'})
        if by_cookie and method == 'post' and not (self.headers.get('X-Client') and self.same_origin()):
            return self.send(403, {'error': 'refused: a signed-in browser request must come from the smartex page itself'})
        name = f'{method}_{url.path.strip("/").replace("/", "_") or "index"}'
        route = getattr(self, name, None)
        if route is None:
            return self.send(404, {'error': f'no endpoint {method.upper()} {url.path}; see GET /api/help'})
        try:
            return route(q)
        except ApiError as e:
            return self.send(e.status, dict(error=str(e), **e.extra))
        except (ValueError, KeyError, UnicodeDecodeError) as e:
            return self.send(400, {'error': f'{type(e).__name__}: {e}'})

    def do_GET(self):
        self.dispatch('get')

    def do_POST(self):
        self.dispatch('post')

    # ---- discovery ----
    def get_index(self, q):
        self.send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')

    def get_api_help(self, q):
        self.send(200, dict(
            name='smartex', version=VERSION,
            auth='send header X-Token: <token from ~/.smartex_token>; name yourself with X-Client: agent:<name>. '
                 'Browsers sign in at /login, which sets a cookie.',
            conventions=['paths are relative to the project folder', 'project may be omitted when only one is served',
                         'writes return the new sha; pass base_sha to avoid overwriting someone else\'s change',
                         'every write is versioned (GET /api/history) and announced on GET /api/events',
                         'to comment on a person\'s text without changing it, use POST /api/notes'],
            endpoints=[dict(method=m, path=p, params=a, description=d) for m, p, a, d in ENDPOINTS],
            projects_dir=str(self.projects_dir),
            features=dict(pdf_text=bool(shutil.which('pdftotext')), pdf_page_images=bool(shutil.which('pdftoppm')),
                          synctex=True, assistant=bool(claude_binary()), new_projects=True)))

    def get_api_projects(self, q):
        self.send(200, [dict(name=p.name, main=p.main_file()) for p in self.projects.values()])

    def post_api_projects(self, q):
        p = create_project(str(self.body().get('name', '')).strip(), self.client)
        self.send(200, dict(name=p.name, path=str(p.root), main=None))

    # ---- files ----
    def get_api_files(self, q):
        p = self.project(q)
        main = p.main_file()
        self.send(200, dict(files=p.files(), main=main, pdf=bool(main) and p.path(main).with_suffix('.pdf').exists()))

    def get_api_file(self, q):
        p = self.project(q)
        data = p.path(self.arg(q, 'path', required=True)).read_bytes()
        text = data.decode('utf-8', errors='replace')
        lines = text.split('\n')
        start, end = self.arg(q, 'start'), self.arg(q, 'end')
        out = dict(path=self.arg(q, 'path'), sha=digest(data), lines=len(lines))
        if start or end:
            s, e = max(1, int(start or 1)), min(len(lines), int(end or len(lines)))
            chunk = lines[s - 1:e]
            out.update(start=s, end=e)
        else:
            s, chunk = 1, lines
        if self.arg(q, 'numbered') == '1':
            out['content'] = '\n'.join(f'{s + i}\t{l}' for i, l in enumerate(chunk))
        else:
            out['content'] = '\n'.join(chunk)
        self.send(200, out)

    def get_api_stat(self, q):
        p = self.project(q)
        out = {}
        for rel in q.get('path', []):
            try:
                out[rel] = digest(p.path(rel).read_bytes())
            except ApiError:
                out[rel] = None
        self.send(200, out)

    def post_api_file(self, q):
        p, b = self.project(q), self.body()
        if 'content' not in b:
            raise ApiError(400, 'body needs "content"')
        sha = p.write(self.arg(q, 'path', required=True), b['content'].encode(), self.client, b.get('base_sha'), b.get('force', False))
        self.send(200, {'sha': sha})

    def post_api_edit(self, q):
        p, b = self.project(q), self.body()
        edits = b.get('edits') or ([dict(old=b['old'], new=b.get('new', ''))] if 'old' in b else None)
        if not edits:
            raise ApiError(400, 'body needs "edits": [{"old": ..., "new": ...}]')
        sha, n = p.edit(self.arg(q, 'path', required=True), edits, self.client, b.get('base_sha'))
        self.send(200, {'sha': sha, 'edits_applied': n})

    def post_api_new(self, q):
        p, b = self.project(q), self.body()
        sha = p.write(self.arg(q, 'path', required=True), b.get('content', '').encode(), self.client, must_not_exist=True)
        self.send(200, {'sha': sha})

    def post_api_rename(self, q):
        p = self.project(q)
        src, dst = self.arg(q, 'path', required=True), self.arg(q, 'to', required=True)
        a, b = p.path(src), p.path(dst, must_exist=False)
        p.editable(b)
        if b.exists():
            raise ApiError(409, f'{dst} already exists')
        with p.lock:
            p._snapshot(a, self.client)
            b.parent.mkdir(parents=True, exist_ok=True)
            os.replace(a, b)
        EVENTS.emit(p.name, 'file_renamed', path=src, to=dst, client=self.client)
        self.send(200, {'path': dst})

    def post_api_delete(self, q):
        p = self.project(q)
        rel = self.arg(q, 'path', required=True)
        target = p.path(rel)
        with p.lock:
            p._snapshot(target, self.client)
            target.unlink()
        EVENTS.emit(p.name, 'file_deleted', path=rel, client=self.client)
        self.send(200, {'deleted': rel, 'restore_with': 'POST /api/history/restore'})

    def get_api_search(self, q):
        p = self.project(q)
        needle = self.arg(q, 'q', required=True)
        ctx = min(int(self.arg(q, 'context', 0)), 10)
        rx = re.compile(needle if self.arg(q, 'regex') == '1' else re.escape(needle))
        only = self.arg(q, 'path')
        hits = []
        for f in p.files():
            if only and f['path'] != only:
                continue
            lines = (p.root / f['path']).read_text(errors='replace').split('\n')
            for i, line in enumerate(lines):
                if rx.search(line):
                    hits.append(dict(path=f['path'], line=i + 1, text=line, before=lines[max(0, i - ctx):i],
                                     after=lines[i + 1:i + 1 + ctx]))
                    if len(hits) >= 500:
                        return self.send(200, dict(hits=hits, truncated=True))
        self.send(200, dict(hits=hits, truncated=False))

    # ---- build ----
    def post_api_compile(self, q):
        p = self.project(q)
        self.send(200, p.compile(self.body().get('main'), self.client))

    def get_api_compile_last(self, q):
        p = self.project(q)
        self.send(200, p.last_compile or {'error': 'nothing compiled since the server started'})

    def get_api_log(self, q):
        p = self.project(q)
        log = p.main_path(self.arg(q, 'main')).with_suffix('.log')
        if not log.exists():
            raise ApiError(404, 'no log yet: compile first')
        self.send(200, log.read_bytes(), 'text/plain; charset=utf-8')

    def _pdf(self, q):
        pdf = self.project(q).main_path(self.arg(q, 'main')).with_suffix('.pdf')
        if not pdf.exists():
            raise ApiError(404, 'no PDF yet: compile first')
        return pdf

    def get_api_pdf(self, q):
        self.send(200, self._pdf(q).read_bytes(), 'application/pdf')

    def get_api_pdf_info(self, q):
        pdf = self._pdf(q)
        st = pdf.stat()
        self.send(200, dict(pages=pdf_pages(pdf), sizes=pdf_page_sizes(pdf), bytes=st.st_size, mtime=st.st_mtime_ns // 1000000))

    def get_api_pdf_text(self, q):
        pdf = self._pdf(q)
        if not shutil.which('pdftotext'):
            raise ApiError(501, 'pdftotext is not installed on the server')
        cmd = ['pdftotext', '-layout']
        page = self.arg(q, 'page')
        if page:
            cmd += ['-f', page, '-l', page]
        out = subprocess.run(cmd + [str(pdf), '-'], capture_output=True, text=True, timeout=60)
        self.send(200, out.stdout.encode(), 'text/plain; charset=utf-8')

    def get_api_pdf_page(self, q):
        pdf = self._pdf(q)
        if not shutil.which('pdftoppm'):
            raise ApiError(501, 'pdftoppm is not installed on the server')
        page, dpi = int(self.arg(q, 'page', 1)), max(30, min(int(self.arg(q, 'dpi', 110)), 300))
        key = (str(pdf), pdf.stat().st_mtime_ns, page, dpi)
        if key in _PAGE_CACHE:
            return self.send(200, _PAGE_CACHE[key], 'image/png')
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-r', str(dpi), '-png', '-singlefile',
                            str(pdf), os.path.join(d, 'page')], check=True, timeout=60)
            png = Path(d, 'page.png')
            if not png.exists():
                raise ApiError(404, f'page {page} does not exist')
            data = png.read_bytes()
        if len(_PAGE_CACHE) > 120:
            _PAGE_CACHE.clear()
        _PAGE_CACHE[key] = data
        self.send(200, data, 'image/png')

    def get_api_synctex_view(self, q):
        p = self.project(q)
        rel, line = self.arg(q, 'path', required=True), int(self.arg(q, 'line', required=True))
        recs = [r for r in synctex_records(p, p.main_path(self.arg(q, 'main'))) if r[1] == rel]
        if not recs:
            raise ApiError(404, f'{rel} does not appear in the SyncTeX data (is it part of the document?)')
        best = min(abs(r[2] - line) for r in recs)
        hits = [r for r in recs if abs(r[2] - line) == best]
        page = max(set(h[0] for h in hits), key=[h[0] for h in hits].count)
        on = [h for h in hits if h[0] == page]
        self.send(200, dict(page=page, x=round(sum(h[3] for h in on) / len(on), 1), y=round(sum(h[4] for h in on) / len(on), 1),
                            matched_line=hits[0][2], approximate=True,
                            units='PostScript points from the top-left corner of the page'))

    def get_api_synctex_edit(self, q):
        p = self.project(q)
        page, x, y = int(self.arg(q, 'page', required=True)), float(self.arg(q, 'x', required=True)), float(self.arg(q, 'y', required=True))
        recs = [r for r in synctex_records(p, p.main_path(self.arg(q, 'main'))) if r[0] == page and r[1]]
        if not recs:
            raise ApiError(404, f'no SyncTeX records on page {page}')
        r = min(recs, key=lambda r: (r[3] - x) ** 2 + (r[4] - y) ** 2)
        self.send(200, dict(path=r[1], line=r[2], approximate=True))

    # ---- history ----
    def get_api_history(self, q):
        p = self.project(q)
        rel = self.arg(q, 'path', required=True)
        self.send(200, dict(path=rel, versions=p.history(rel)))

    def _version(self, q):
        p = self.project(q)
        rel, vid = self.arg(q, 'path', required=True), self.arg(q, 'id', required=True)
        f = (p._history_dir(rel) / vid).resolve()
        if p._history_dir(rel).resolve() not in f.parents or not f.is_file():
            raise ApiError(404, f'no version {vid} of {rel}')
        return p, rel, f

    def get_api_history_get(self, q):
        _, rel, f = self._version(q)
        data = f.read_bytes()
        self.send(200, dict(path=rel, id=f.name, sha=digest(data), content=data.decode('utf-8', errors='replace')))

    def post_api_history_restore(self, q):
        p, rel, f = self._version(q)
        sha = p.write(rel, f.read_bytes(), self.client, force=True)
        self.send(200, dict(path=rel, restored=f.name, sha=sha))

    # ---- people and agents together ----
    def get_api_presence(self, q):
        self.send(200, self.project(q).presence)

    def post_api_presence(self, q):
        p, b = self.project(q), self.body()
        b = {k: b[k] for k in ('path', 'line', 'ch', 'selection') if k in b}
        if 'selection' in b:
            b['selection'] = str(b['selection'])[:2000]
        b['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        p.presence[self.client] = b
        self.send(200, {'ok': True})

    def post_api_ui_open(self, q):
        p, b = self.project(q), self.body()
        rel = b.get('path') or ''
        p.path(rel)
        EVENTS.emit(p.name, 'ui_open', path=rel, line=int(b.get('line') or 1), message=str(b.get('message', ''))[:500], client=self.client)
        self.send(200, {'ok': True})

    def get_api_notes(self, q):
        p = self.project(q)
        notes = p.notes()
        if self.arg(q, 'all') != '1':
            notes = [n for n in notes if not n.get('resolved')]
        if self.arg(q, 'path'):
            notes = [n for n in notes if n['path'] == self.arg(q, 'path')]
        self.send(200, dict(notes=notes))

    def post_api_notes(self, q):
        p, b = self.project(q), self.body()
        p.path(b.get('path') or '')
        if not b.get('text'):
            raise ApiError(400, 'body needs "text"')
        with p.lock:
            notes = p.notes()
            note = dict(id=secrets.token_hex(4), path=b['path'], line=int(b.get('line') or 1), text=str(b['text'])[:4000],
                        author=self.client, created=time.strftime('%Y-%m-%dT%H:%M:%S'), resolved=False)
            notes.append(note)
            p.save_notes(notes)
        EVENTS.emit(p.name, 'note_added', id=note['id'], path=note['path'], line=note['line'], client=self.client)
        self.send(200, note)

    def post_api_notes_resolve(self, q):
        p, b = self.project(q), self.body()
        with p.lock:
            notes = p.notes()
            hit = [n for n in notes if n['id'] == b.get('id')]
            if not hit:
                raise ApiError(404, f'no note {b.get("id")}')
            hit[0].update(resolved=True, resolved_by=self.client, resolved_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
            p.save_notes(notes)
        EVENTS.emit(p.name, 'note_resolved', id=b['id'], client=self.client)
        self.send(200, hit[0])

    # ---- the assistant ----
    def agent(self, q):
        return agent_for(self.project(q), self.port)

    def get_api_agent(self, q):
        self.send(200, self.agent(q).info())

    def post_api_agent_start(self, q):
        a = self.agent(q)
        a.start(self.body().get('model'))
        self.send(200, a.info())

    def post_api_agent_config(self, q):
        a, b = self.agent(q), self.body()
        a.configure(b.get('model'), b.get('effort'))
        self.send(200, a.info())

    def post_api_agent_stop(self, q):
        a = self.agent(q)
        a.stop()
        self.send(200, a.info())

    def post_api_agent_reset(self, q):
        a = self.agent(q)
        a.reset()
        self.send(200, a.info())

    def post_api_agent_message(self, q):
        p, b = self.project(q), self.body()
        context = None
        if b.get('context', True):
            humans = sorted(((k, v) for k, v in p.presence.items() if k.startswith('human:')), key=lambda kv: kv[1].get('updated', ''))
            if humans:
                v = humans[-1][1]
                context = f"the person is viewing {v.get('path')} at line {v.get('line')}"
                if v.get('selection'):
                    context += f"; selected text: {v['selection'][:1500]!r}"
        a = self.agent(q)
        a.send(str(b.get('text', '')), self.client, context)
        self.send(200, a.info())

    def get_api_agent_transcript(self, q):
        a = self.agent(q)
        since = int(self.arg(q, 'since', -1))
        self.send(200, dict(entries=[e for e in a.entries if e['index'] > since], status=a.status, busy=a.busy))

    def get_api_events(self, q):
        p = self.project(q)
        items, last = EVENTS.since(p.name, int(self.arg(q, 'since', 0)), float(self.arg(q, 'timeout', 0)))
        self.send(200, dict(events=items, last_seq=last))


# ---------------------------------------------------------------------------------------------------
# new projects, and the registry that keeps them served after a restart
# ---------------------------------------------------------------------------------------------------
CREATE_LOCK = threading.Lock()


def create_project(name, client):
    """Make an empty project folder in the projects folder and serve it from now on."""
    if not PROJECT_NAME.fullmatch(name):
        raise ApiError(400, 'a project name is 1 to 64 letters, digits, ".", "_" or "-", starting with a letter or digit')
    with CREATE_LOCK:
        if name in Handler.projects:
            raise ApiError(409, f'there is already a project called {name}')
        folder = Handler.projects_dir / name
        if folder.exists():
            raise ApiError(409, f'{folder} already exists; choose another name')
        Handler.projects_dir.mkdir(parents=True, exist_ok=True)
        folder.mkdir()
        p = Project(folder)
        # a new dict rather than an insertion, so a request iterating the old one is not disturbed
        Handler.projects = {**Handler.projects, name: p}
        if Handler.registry:
            remember(Handler.registry, [p.root])
    for other in Handler.projects:
        EVENTS.emit(other, 'project_created', name=name, path=str(p.root), client=client)
    return p


def registered(registry):
    if not registry.exists():
        return []
    return [Path(l.strip()).expanduser() for l in registry.read_text().splitlines() if l.strip() and not l.lstrip().startswith('#')]


def remember(registry, folders):
    """Add folders to the registry, keeping the ones already in it."""
    known = {f.resolve() for f in registered(registry)}
    new = [str(f) for f in folders if f.resolve() not in known]
    if new:
        fresh = not registry.exists()
        with open(registry, 'a') as fh:
            fh.write((REGISTRY_HEADER if fresh else '') + ''.join(f + '\n' for f in new))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', action='append', default=[], help='project folder (repeat for several)')
    ap.add_argument('--projects-dir', default=os.environ.get('SMARTEX_PROJECTS_DIR') or str(Handler.projects_dir),
                    help='where new projects are made (default ~/smartex-projects, or $SMARTEX_PROJECTS_DIR)')
    ap.add_argument('--registry', metavar='FILE', help='also serve the folders listed in FILE, and record every served '
                                                        'and newly created project folder there')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--public', metavar='HOST:PORT', help='a second listener for a reverse proxy, e.g. 0.0.0.0:5005')
    ap.add_argument('--allow-from', action='append', default=[], metavar='ADDR',
                    help='address or network the public listener accepts (repeat); the loopback is always accepted')
    args = ap.parse_args()
    public = None
    if args.public:
        host, _, port = args.public.rpartition(':')
        if not host or not port.isdigit():
            sys.exit('--public needs HOST:PORT, for example 0.0.0.0:5005')
        if not args.allow_from:
            sys.exit('--public needs at least one --allow-from (the address your reverse proxy connects from)')
        try:
            Handler.allow_from = [ipaddress.ip_network(a, strict=False) for a in args.allow_from]
        except ValueError as e:
            sys.exit(f'--allow-from: {e}')
        public = (host, int(port))
    roots = []
    for r in args.root:
        if not Path(r).expanduser().is_dir():
            sys.exit(f'not a folder: {r}')
        roots.append(Path(r).expanduser().resolve())
    registry = Path(args.registry).expanduser() if args.registry else None
    if registry:
        for r in registered(registry):
            if r.is_dir():
                roots.append(r.resolve())
            else:
                print(f'skipping {r} (listed in {registry}): not a folder', flush=True)
    roots = list(dict.fromkeys(roots))
    if not roots:
        sys.exit('no projects to serve: give --root DIR' + (f', or list folders in {registry}' if registry else ''))
    projects = {}
    for r in roots:
        p = Project(r)
        name, k = p.name, 2
        while name in projects:
            name, k = f'{p.name}-{k}', k + 1
        p.name = name
        projects[name] = p
    if registry:
        remember(registry, roots)
    Handler.registry, Handler.projects_dir = registry, Path(args.projects_dir).expanduser().resolve()
    token_file = Path.home() / '.smartex_token'
    if token_file.exists() and token_file.stat().st_mode & 0o077 == 0:
        token = token_file.read_text().strip()
    else:
        token = secrets.token_urlsafe(24)
        token_file.write_text(token + '\n')
        token_file.chmod(0o600)
    Handler.projects, Handler.token, Handler.port = projects, token, args.port
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.daemon_threads, srv.public = True, False
    if public:
        psrv = http.server.ThreadingHTTPServer(public, Handler)
        psrv.daemon_threads, psrv.public = True, True
        threading.Thread(target=psrv.serve_forever, daemon=True).start()
    import atexit
    import signal
    atexit.register(lambda: [a.stop() for a in AGENTS.values()])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))     # so atexit runs on `smartex.sh stop`
    print(f'smartex {VERSION} serving {", ".join(projects)} on 127.0.0.1:{args.port}', flush=True)
    print(f'new projects go to {Handler.projects_dir}' + (f'; the list of projects is kept in {registry}' if registry else ''),
          flush=True)
    exe = claude_binary()
    print(f'assistant: {exe}' if exe else 'assistant: unavailable, the claude command was not found (set SMARTEX_CLAUDE)', flush=True)
    if public:
        print(f'also listening on {public[0]}:{public[1]} for a reverse proxy; accepting the loopback and '
              f'{", ".join(str(n) for n in Handler.allow_from)}', flush=True)
    print(f'open: http://localhost:{args.port}/?token={token}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
