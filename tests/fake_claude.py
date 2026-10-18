#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json --output-format stream-json`, for testing smartex's
assistant without using Claude: it answers every message instantly with a canned reply (including math).

    SMARTEX_CLAUDE=tests/fake_claude.py ./smartex.sh start PROJECT
"""
import json
import sys
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
    say({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'name': 'mcp__smartex__list_files', 'input': {}},
        {'type': 'text', 'text': f'Reply {n} to: {question}\n\nWith $E = \\sigma\\int ds\\,dy$ and $$c_m = 2(M/\\sigma - 1).$$'}]}})
    say({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'duration_ms': 120, 'session_id': sid})
