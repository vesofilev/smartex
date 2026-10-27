"""Python client for the smartex API, shared by the `smartex` command-line tool and the MCP server.

Configuration (arguments override environment variables):
  SMARTEX_URL      default http://127.0.0.1:8765
  SMARTEX_TOKEN    default: contents of ~/.smartex_token
  SMARTEX_CLIENT   who you are, recorded with every change; default agent:cli
  SMARTEX_PROJECT  project name; may be omitted when the server serves a single project
Standard library only.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class SmartexError(Exception):
    def __init__(self, status, info):
        self.status, self.info = status, info
        super().__init__(f'HTTP {status}: {info.get("error", info)}')


class Client:
    def __init__(self, url=None, token=None, client=None, project=None):
        self.url = (url or os.environ.get('SMARTEX_URL') or 'http://127.0.0.1:8765').rstrip('/')
        token = token or os.environ.get('SMARTEX_TOKEN')
        if not token:
            f = Path.home() / '.smartex_token'
            token = f.read_text().strip() if f.exists() else ''
        self.token = token
        self.client = client or os.environ.get('SMARTEX_CLIENT') or 'agent:cli'
        self.project = project or os.environ.get('SMARTEX_PROJECT')

    def request(self, method, path, params=None, body=None, raw=False, timeout=300):
        params = {k: v for k, v in (params or {}).items() if v is not None and v is not False}
        params = {k: ('1' if v is True else v) for k, v in params.items()}
        if self.project and 'project' not in params:
            params['project'] = self.project
        url = self.url + path + ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')
        data = json.dumps(body).encode() if body is not None else (b'{}' if method == 'POST' else None)
        req = urllib.request.Request(url, data=data, method=method, headers={
            'X-Token': self.token, 'X-Client': self.client, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload, ctype = r.read(), r.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                info = json.loads(payload)
            except ValueError:
                info = {'error': payload.decode(errors='replace')}
            raise SmartexError(e.code, info)
        except urllib.error.URLError as e:
            raise SmartexError(0, {'error': f'cannot reach smartex at {self.url}: {e.reason}'})
        if raw:
            return payload
        return json.loads(payload) if 'json' in ctype else payload.decode('utf-8', errors='replace')

    # discovery
    def help(self):
        return self.request('GET', '/api/help')

    def projects(self):
        return self.request('GET', '/api/projects')

    def create_project(self, name):
        return self.request('POST', '/api/projects', body={'name': name})

    def files(self):
        return self.request('GET', '/api/files')

    # reading and writing
    def read(self, path, start=None, end=None, numbered=False):
        return self.request('GET', '/api/file', dict(path=path, start=start, end=end, numbered=numbered))

    def write(self, path, content, base_sha=None, force=False):
        return self.request('POST', '/api/file', dict(path=path), dict(content=content, base_sha=base_sha, force=force))

    def edit(self, path, edits, base_sha=None):
        return self.request('POST', '/api/edit', dict(path=path), dict(edits=edits, base_sha=base_sha))

    def new(self, path, content=''):
        return self.request('POST', '/api/new', dict(path=path), dict(content=content))

    def rename(self, path, to):
        return self.request('POST', '/api/rename', dict(path=path, to=to))

    def delete(self, path):
        return self.request('POST', '/api/delete', dict(path=path))

    def search(self, query, regex=False, context=0, path=None):
        return self.request('GET', '/api/search', dict(q=query, regex=regex, context=context or None, path=path))

    # building and looking at the result
    def compile(self, main=None):
        return self.request('POST', '/api/compile', body=dict(main=main))

    def last_compile(self):
        return self.request('GET', '/api/compile/last')

    def log(self):
        return self.request('GET', '/api/log')

    def pdf(self):
        return self.request('GET', '/api/pdf', raw=True)

    def pdf_info(self):
        return self.request('GET', '/api/pdf/info')

    def pdf_text(self, page=None):
        return self.request('GET', '/api/pdf/text', dict(page=page))

    def pdf_page(self, page=1, dpi=110):
        return self.request('GET', '/api/pdf/page', dict(page=page, dpi=dpi), raw=True)

    def where(self, path, line):
        return self.request('GET', '/api/synctex/view', dict(path=path, line=line))

    def which(self, page, x, y):
        return self.request('GET', '/api/synctex/edit', dict(page=page, x=x, y=y))

    # history
    def history(self, path):
        return self.request('GET', '/api/history', dict(path=path))

    def version(self, path, vid):
        return self.request('GET', '/api/history/get', dict(path=path, id=vid))

    def restore(self, path, vid):
        return self.request('POST', '/api/history/restore', dict(path=path, id=vid))

    # working with people
    def presence(self):
        return self.request('GET', '/api/presence')

    def report_presence(self, path, line, ch=None, selection=None):
        return self.request('POST', '/api/presence', body=dict(path=path, line=line, ch=ch, selection=selection))

    def open_in_editor(self, path, line=None, message=None):
        return self.request('POST', '/api/ui/open', body=dict(path=path, line=line, message=message))

    def notes(self, path=None, include_resolved=False):
        return self.request('GET', '/api/notes', dict(path=path, all=include_resolved))

    def add_note(self, path, line, text):
        return self.request('POST', '/api/notes', body=dict(path=path, line=line, text=text))

    def resolve_note(self, note_id):
        return self.request('POST', '/api/notes/resolve', body=dict(id=note_id))

    # the assistant (persistent Claude Code session on the server)
    def agent(self):
        return self.request('GET', '/api/agent')

    def ask(self, text, context=True):
        return self.request('POST', '/api/agent/message', body=dict(text=text, context=context))

    def transcript(self, since=-1):
        return self.request('GET', '/api/agent/transcript', dict(since=since))

    def agent_stop(self):
        return self.request('POST', '/api/agent/stop')

    def agent_reset(self):
        return self.request('POST', '/api/agent/reset')

    def events(self, since=0, timeout=0):
        return self.request('GET', '/api/events', dict(since=since, timeout=timeout), timeout=timeout + 30)
