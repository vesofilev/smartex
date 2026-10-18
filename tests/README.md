# Browser tests

Headless-Chromium checks of the editor: layout (no overflow, no sideways scroll, file list, themes, narrow
windows), JavaScript errors, and the human-agent interactions (live reload of agent edits, notes, conflict
banner, clickable compile errors). They need Playwright:

```sh
python3 -m venv ~/.venvs/uitest && ~/.venvs/uitest/bin/pip install playwright   # 1.48 on Ubuntu 20.04
~/.venvs/uitest/bin/python -m playwright install chromium
```

Run them against a smartex serving a scratch copy of a project (the interaction test edits files):

```sh
./smartex.sh start /tmp/scratch-copy-of-a-paper
URL="http://127.0.0.1:8765/?token=$(cat ~/.smartex_token)"
~/.venvs/uitest/bin/python tests/ui_layout.py "$URL" /tmp/shots
~/.venvs/uitest/bin/python tests/ui_interactions.py "$URL" /tmp/shots ./smartex
```

~/.venvs/uitest/bin/python tests/ui_sync.py "$URL" /tmp/shots     # forward/inverse SyncTeX, Cmd-J, zoom

Screenshots land in the output folder.
