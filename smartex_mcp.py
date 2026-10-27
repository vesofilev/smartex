#!/usr/bin/env python3
"""smartex MCP server: exposes a smartex workspace to AI agents as Model Context Protocol tools (stdio).

    claude mcp add smartex -- python3 /path/to/smartex_mcp.py
    # optional: --url http://127.0.0.1:8765 --client agent:claude-code --project NAME

It talks to a running smartex server, so agents and people share one workspace: the same files, the
same conflict checks, history and event feed. Configuration also via SMARTEX_URL, SMARTEX_TOKEN
(or ~/.smartex_token), SMARTEX_CLIENT (default agent:mcp) and SMARTEX_PROJECT. Standard library only.
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from smartex_client import Client, SmartexError  # noqa: E402

SERVER_INFO = {'name': 'smartex', 'version': '0.3.0'}


def S(props, required=()):
    props = dict(props, project={'type': 'string', 'description': 'Project name; omit when only one project is served.'})
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


STR, INT, BOOL, NUM = {'type': 'string'}, {'type': 'integer'}, {'type': 'boolean'}, {'type': 'number'}
EDITS = {'type': 'array', 'items': {'type': 'object', 'properties': {'old': STR, 'new': STR}, 'required': ['old', 'new']},
         'description': 'Applied in order and atomically. Each "old" must occur exactly once in the file.'}

TOOLS = [
    ('list_projects', 'List the LaTeX projects this workspace serves and their main files.', S({}),
     lambda c, a: c.projects()),
    ('create_project', 'Create a new, empty project (a folder in the workspace\'s projects folder) when the person asks '
     'for one. Then work in it by passing its name as "project" to the other tools, starting with create_file for its '
     'main .tex file.',
     {'type': 'object', 'properties': {'name': {'type': 'string', 'description': '1 to 64 letters, digits, ".", "_" or "-"'}},
      'required': ['name'], 'additionalProperties': False},
     lambda c, a: c.create_project(a['name'])),
    ('list_files', 'List editable files (path, size, mtime, sha), the main .tex file, and whether a PDF exists.', S({}),
     lambda c, a: c.files()),
    ('read_file', 'Read a file, optionally a 1-based inclusive line range, optionally with line numbers. '
     'Returns content and sha; pass the sha as base_sha when writing back.',
     S({'path': STR, 'start': INT, 'end': INT, 'numbered': BOOL}, ['path']),
     lambda c, a: c.read(a['path'], a.get('start'), a.get('end'), a.get('numbered', False))),
    ('edit_file', 'Change an existing file by exact find-and-replace. Prefer this over write_file. '
     'Fails without changing anything if an "old" string is missing or ambiguous, or if base_sha is stale.',
     S({'path': STR, 'edits': EDITS, 'base_sha': STR}, ['path', 'edits']),
     lambda c, a: c.edit(a['path'], a['edits'], a.get('base_sha'))),
    ('write_file', 'Create a file or replace its whole content. With base_sha the write is refused if someone '
     'changed the file since you read it.', S({'path': STR, 'content': STR, 'base_sha': STR, 'force': BOOL}, ['path', 'content']),
     lambda c, a: c.write(a['path'], a['content'], a.get('base_sha'), a.get('force', False))),
    ('create_file', 'Create a new file (fails if it exists).', S({'path': STR, 'content': STR}, ['path']),
     lambda c, a: c.new(a['path'], a.get('content', ''))),
    ('rename_file', 'Rename a file.', S({'path': STR, 'to': STR}, ['path', 'to']),
     lambda c, a: c.rename(a['path'], a['to'])),
    ('delete_file', 'Delete a file. Its last content stays in history and can be restored with restore_version.',
     S({'path': STR}, ['path']), lambda c, a: c.delete(a['path'])),
    ('search', 'Search all editable files for a string (or a regular expression with regex=true). '
     'Returns path, line, text and optional context lines.',
     S({'query': STR, 'regex': BOOL, 'context': INT, 'path': STR}, ['query']),
     lambda c, a: c.search(a['query'], a.get('regex', False), a.get('context', 0), a.get('path'))),
    ('compile', 'Typeset the document with latexmk. Returns ok, errors with file and line, warnings, overfull '
     'boxes and the page count.', S({'main': STR}), lambda c, a: c.compile(a.get('main'))),
    ('last_compile', 'The most recent compile result, whoever started it.', S({}), lambda c, a: c.last_compile()),
    ('get_log', 'The LaTeX log (last tail_lines lines, default 200).', S({'tail_lines': INT}),
     lambda c, a: '\n'.join(c.log().splitlines()[-int(a.get('tail_lines', 200)):])),
    ('pdf_text', 'Text of the compiled PDF, or of one page.', S({'page': INT}), lambda c, a: c.pdf_text(a.get('page'))),
    ('view_pdf_page', 'Render one page of the compiled PDF as an image so you can check layout, equations and figures.',
     S({'page': INT, 'dpi': INT}, ['page']), None),
    ('source_to_pdf', 'Find where a source line appears in the PDF (page and approximate position).',
     S({'path': STR, 'line': INT}, ['path', 'line']), lambda c, a: c.where(a['path'], a['line'])),
    ('pdf_to_source', 'Find the source line that produced a point on a PDF page (points from the top left).',
     S({'page': INT, 'x': NUM, 'y': NUM}, ['page', 'x', 'y']), lambda c, a: c.which(a['page'], a['x'], a['y'])),
    ('file_history', 'Earlier versions of a file, newest first, with who overwrote each one.', S({'path': STR}, ['path']),
     lambda c, a: c.history(a['path'])),
    ('get_version', 'Content of an earlier version of a file.', S({'path': STR, 'id': STR}, ['path', 'id']),
     lambda c, a: c.version(a['path'], a['id'])),
    ('restore_version', 'Restore an earlier version of a file (the current content is versioned first).',
     S({'path': STR, 'id': STR}, ['path', 'id']), lambda c, a: c.restore(a['path'], a['id'])),
    ('get_presence', 'What each person and agent is looking at: open file, cursor line, selected text. '
     'Use it to understand requests like "fix this paragraph".', S({}), lambda c, a: c.presence()),
    ('open_in_editor', 'Ask the people using the browser editor to open a file at a line, with a short message.',
     S({'path': STR, 'line': INT, 'message': STR}, ['path']),
     lambda c, a: c.open_in_editor(a['path'], a.get('line'), a.get('message'))),
    ('list_notes', 'Review notes anchored to lines (open ones unless include_resolved).',
     S({'path': STR, 'include_resolved': BOOL}), lambda c, a: c.notes(a.get('path'), a.get('include_resolved', False))),
    ('add_note', 'Leave a review note on a line instead of editing someone\'s text (for example a suggested '
     'correction for a human-written sentence).', S({'path': STR, 'line': INT, 'text': STR}, ['path', 'line', 'text']),
     lambda c, a: c.add_note(a['path'], a['line'], a['text'])),
    ('resolve_note', 'Mark a review note as resolved.', S({'id': STR}, ['id']), lambda c, a: c.resolve_note(a['id'])),
    ('wait_for_events', 'Changes since a sequence number: file edits by people or agents, compiles, notes. '
     'Blocks up to timeout seconds (max 60) until something happens. Start with since=0.',
     S({'since': INT, 'timeout': NUM}), lambda c, a: c.events(a.get('since', 0), a.get('timeout', 0))),
]
BY_NAME = {t[0]: t for t in TOOLS}


def call_tool(client, name, args):
    if name not in BY_NAME:
        return {'content': [{'type': 'text', 'text': f'unknown tool {name}'}], 'isError': True}
    if args.get('project'):
        client = Client(client.url, client.token, client.client, args['project'])
    try:
        if name == 'view_pdf_page':
            png = client.pdf_page(int(args['page']), int(args.get('dpi', 110)))
            return {'content': [{'type': 'image', 'data': base64.b64encode(png).decode(), 'mimeType': 'image/png'}]}
        result = BY_NAME[name][3](client, args)
        text = result if isinstance(result, str) else json.dumps(result, indent=1, ensure_ascii=False)
        return {'content': [{'type': 'text', 'text': text}]}
    except SmartexError as e:
        return {'content': [{'type': 'text', 'text': json.dumps(dict(status=e.status, **e.info), ensure_ascii=False)}], 'isError': True}
    except (KeyError, ValueError, TypeError) as e:
        return {'content': [{'type': 'text', 'text': f'bad arguments: {e}'}], 'isError': True}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url'), ap.add_argument('--token'), ap.add_argument('--project')
    ap.add_argument('--client', default=os.environ.get('SMARTEX_CLIENT', 'agent:mcp'))
    a = ap.parse_args()
    client = Client(a.url, a.token, a.client, a.project)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
        if mid is None:          # notification (e.g. notifications/initialized): no reply
            continue
        if method == 'initialize':
            result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                      'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': SERVER_INFO,
                      'instructions': 'Shared LaTeX workspace. Read with read_file, change with edit_file, check with '
                                      'compile and view_pdf_page. Comment on human-written text with add_note instead of '
                                      'editing it. Use get_presence to see what the person is looking at.'}
        elif method == 'tools/list':
            result = {'tools': [{'name': n, 'description': d, 'inputSchema': s} for n, d, s, _ in TOOLS]}
        elif method == 'tools/call':
            result = call_tool(client, params.get('name'), params.get('arguments') or {})
        elif method == 'ping':
            result = {}
        else:
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'unknown method {method}'}}) + '\n')
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': result}, ensure_ascii=False) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
