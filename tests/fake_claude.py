#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json --output-format stream-json`, for testing smartex's
assistant without using Claude: it answers every message instantly with a canned reply (including math).

    SMARTEX_CLAUDE=tests/fake_claude.py ./smartex.sh start PROJECT
"""
import json
import sys
import time
import uuid

args = sys.argv[1:]
sid = args[args.index('--resume') + 1] if '--resume' in args else str(uuid.uuid4())
model = args[args.index('--model') + 1] if '--model' in args else 'fake-default'
say = lambda obj: (sys.stdout.write(json.dumps(obj) + '\n'), sys.stdout.flush())
say({'type': 'system', 'subtype': 'init', 'session_id': sid, 'model': model, 'permissionMode': 'dontAsk', 'apiKeySource': 'none'})
for n, line in enumerate(sys.stdin, 1):
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    text = msg.get('message', {}).get('content', '')
    text = text if isinstance(text, str) else json.dumps(text)
    question = text.split(']\n\n', 1)[-1]
    slow = 'slow' in question                      # "slow" questions take a few visible steps
    steps = [('mcp__smartex__read_file', {'path': 'main.tex', 'start': 1, 'end': 20}, True, '1\t% Working manuscript ...'),
             ('mcp__smartex__compile', {}, True, '{"ok": true, "pages": 3, "errors": []}'),
             ('mcp__smartex__view_pdf_page', {'page': 9}, False, '{"status": 404, "error": "page 9 does not exist"}')] if slow else \
            [('mcp__smartex__list_files', {}, True, '{"files": ["main.tex"], "main": "main.tex"}')]
    if slow:
        say({'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'Read the file, compile, then look at the page.'}]}})
    for k, (name, inp, ok, out) in enumerate(steps):
        tid = f'toolu_{n}_{k}'
        say({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}})
        if slow:
            time.sleep(1.0)
        say({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': tid, 'is_error': not ok, 'content': out}]}})
    say({'type': 'assistant', 'message': {'content': [
        {'type': 'text', 'text': f'Reply {n} to: {question}\n\nWith $E = \\sigma\\int ds\\,dy$ and $$c_m = 2(M/\\sigma - 1).$$'}]}})
    say({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'duration_ms': 120, 'session_id': sid})
