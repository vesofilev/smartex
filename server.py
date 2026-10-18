#!/usr/bin/env python3
"""smartex server: a LaTeX workspace that people and AI agents share.

    python3 server.py --root DIR [--root DIR ...] [--port 8765]

People use the browser editor (index.html). Agents use the same JSON API, either directly, through the
`smartex` command-line client, or through the MCP server (`smartex_mcp.py`). GET /api/help describes
every endpoint. The server listens on 127.0.0.1 only; reach it from another computer through an SSH
tunnel. Every request must carry the access token from ~/.smartex_token (header X-Token or ?token=),
and should name its author in the X-Client header (for example "human:browser" or "agent:claude-code"):
every write is attributed, versioned and announced on the event feed.

Standard library only. Optional extras: pdftotext and pdftoppm (poppler) enable PDF text and page images.
"""
import argparse
import gzip
import hashlib
import http.server
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

VERSION = '0.2.0'
EDITABLE = {'.tex', '.bib', '.sty', '.cls', '.bst', '.md', '.txt', '.cfg'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.smartex'}
HERE = Path(__file__).resolve().parent
# extra directory searched for latexmk before PATH (a user-space TeX Live, for example)
TEXBIN = Path(os.environ.get('SMARTEX_TEXBIN', str(Path.home() / 'texlive' / '2026' / 'bin' / 'x86_64-linux')))
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
        if TEXBIN.is_dir():
            env['PATH'] = f'{TEXBIN}:{env.get("PATH", "")}'
        cmd = ['latexmk', '-pdf', '-interaction=nonstopmode', '-file-line-error', '-synctex=1', main_path.name]
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


def synctex_records(project, main_path):
    f = main_path.with_suffix('.synctex.gz')
    if not f.exists():
        raise ApiError(404, 'no SyncTeX data yet: compile first')
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
# HTTP API
# ---------------------------------------------------------------------------------------------------
ENDPOINTS = [
    # method, path, parameters, description
    ('GET', '/api/help', '', 'This catalogue.'),
    ('GET', '/api/projects', '', 'Projects served, with their main file.'),
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
    ('GET', '/api/pdf/info', 'project, [main]', 'Pages, size and modification time of the PDF.'),
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
    ('GET', '/api/events', 'project, [since=SEQ], [timeout=S <= 60]', 'Change feed (file_written, file_created, file_renamed, file_deleted, compile_started, compile_finished, note_added, note_resolved, ui_open, presence). Blocks until something happens or timeout.'),
]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f'smartex/{VERSION}'
    projects = {}
    token = ''

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s %s\n' % (time.strftime('%H:%M:%S'), self.headers.get('X-Client', '-') if self.headers else '-', fmt % args))

    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
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

    def dispatch(self, method):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        supplied = self.headers.get('X-Token') or self.arg(q, 'token', '')
        if not secrets.compare_digest(supplied, self.token):
            return self.send(403, {'error': 'missing or wrong access token (header X-Token or ?token=); it is in ~/.smartex_token'})
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
            auth='send header X-Token: <token from ~/.smartex_token>; name yourself with X-Client: agent:<name>',
            conventions=['paths are relative to the project folder', 'project may be omitted when only one is served',
                         'writes return the new sha; pass base_sha to avoid overwriting someone else\'s change',
                         'every write is versioned (GET /api/history) and announced on GET /api/events',
                         'to comment on a person\'s text without changing it, use POST /api/notes'],
            endpoints=[dict(method=m, path=p, params=a, description=d) for m, p, a, d in ENDPOINTS],
            features=dict(pdf_text=bool(shutil.which('pdftotext')), pdf_page_images=bool(shutil.which('pdftoppm')),
                          synctex=True)))

    def get_api_projects(self, q):
        self.send(200, [dict(name=p.name, main=p.main_file()) for p in self.projects.values()])

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
        self.send(200, dict(pages=pdf_pages(pdf), bytes=st.st_size, mtime=int(st.st_mtime)))

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
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-r', str(dpi), '-png', '-singlefile',
                            str(pdf), os.path.join(d, 'page')], check=True, timeout=60)
            png = Path(d, 'page.png')
            if not png.exists():
                raise ApiError(404, f'page {page} does not exist')
            self.send(200, png.read_bytes(), 'image/png')

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

    def get_api_events(self, q):
        p = self.project(q)
        items, last = EVENTS.since(p.name, int(self.arg(q, 'since', 0)), float(self.arg(q, 'timeout', 0)))
        self.send(200, dict(events=items, last_seq=last))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', action='append', required=True, help='project folder (repeat for several)')
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args()
    projects = {}
    for r in args.root:
        p = Project(Path(r).expanduser())
        if not p.root.is_dir():
            sys.exit(f'not a folder: {r}')
        name, k = p.name, 2
        while name in projects:
            name, k = f'{p.name}-{k}', k + 1
        p.name = name
        projects[name] = p
    token_file = Path.home() / '.smartex_token'
    if token_file.exists() and token_file.stat().st_mode & 0o077 == 0:
        token = token_file.read_text().strip()
    else:
        token = secrets.token_urlsafe(24)
        token_file.write_text(token + '\n')
        token_file.chmod(0o600)
    Handler.projects, Handler.token = projects, token
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.daemon_threads = True
    print(f'smartex {VERSION} serving {", ".join(projects)} on 127.0.0.1:{args.port}', flush=True)
    print(f'open: http://localhost:{args.port}/?token={token}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
