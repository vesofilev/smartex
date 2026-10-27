# smartex for AI agents

smartex is a LaTeX workspace that you share with people. They use a browser editor; you use the same
operations through MCP tools, the `smartex` command-line tool, or the HTTP API. Everything you change is
attributed to you, versioned, and shown to the people working in the editor as it happens.

## Connecting

The server must be running (`./smartex.sh start PROJECT_DIR`, or `./smartex.sh start` for the projects it
served before). Then pick one:

- **MCP** (preferred): `claude mcp add smartex -- python3 /path/to/smartex_mcp.py --client agent:<your-name>`
- **CLI**: `./smartex <command>`; run `./smartex -h` for the list. Structured output is JSON.
- **HTTP**: `GET http://127.0.0.1:8765/api/help` with header `X-Token: <contents of ~/.smartex_token>`.

Always name yourself (`--client agent:<name>`, `SMARTEX_CLIENT`, or the `X-Client` header). People see
that name next to every change you make.

Configuration: `SMARTEX_URL` (default `http://127.0.0.1:8765`), `SMARTEX_TOKEN` (default
`~/.smartex_token`), `SMARTEX_CLIENT`, `SMARTEX_PROJECT` (only needed when several projects are served).

## Working conventions

1. **Orient.** `list_files` gives the main file. `get_presence` tells you which file, line and selection
   the person is looking at; use it to resolve requests like "fix this paragraph".
2. **Read before you write.** `read_file` (with `numbered`, and a line range for long files) returns a
   `sha`. Pass it back as `base_sha`; the write is refused with 409 if someone changed the file meanwhile.
   Re-read and retry rather than forcing.
3. **Change with `edit_file`**, not `write_file`. Each `old` string must occur exactly once (else 422 and
   nothing changes); include enough surrounding text to make it unique. Several edits in one call apply
   atomically.
4. **Check your work.** `compile` returns `ok`, errors with file and line, warnings and overfull boxes.
   Then look at the result: `view_pdf_page` returns an image of a page; `source_to_pdf` tells you which
   page a source line lands on; `pdf_text` gives the typeset text.
5. **Respect authorship.** When text was written by a person and you were not asked to rewrite it, leave
   a review note (`add_note` on the line) instead of editing. Notes appear in the margin of their editor.
6. **Show, don't hunt.** `open_in_editor` scrolls the person's editor to a line, with a short message.
7. **Wait instead of polling.** `wait_for_events(since=last_seq, timeout=30)` returns as soon as someone
   saves, compiles or comments. Start with `since=0` and keep the returned `last_seq`.
8. **New projects only on request.** When the person asks for one, `create_project` makes an empty folder
   and serves it; then pass its name as `project` to the other tools, starting with `create_file`.
9. **Undo is always possible.** `file_history` lists earlier versions with who overwrote each one;
   `restore_version` brings one back. Deleted files can be restored the same way.

## Operations

| goal | MCP tool | CLI | HTTP |
|---|---|---|---|
| list projects | `list_projects` | `projects` | `GET /api/projects` |
| new, empty project | `create_project` | `new-project NAME` | `POST /api/projects` |
| list files | `list_files` | `files` | `GET /api/files` |
| read | `read_file` | `cat FILE [--start --end --numbered]` | `GET /api/file` |
| exact edit | `edit_file` | `edit FILE --old A --new B [--base SHA]` | `POST /api/edit` |
| write whole file | `write_file` | `write FILE < content` | `POST /api/file` |
| create, rename, delete | `create_file`, `rename_file`, `delete_file` | `new`, `rename`, `delete` | `POST /api/new`, `/api/rename`, `/api/delete` |
| search | `search` | `search QUERY [--regex --context N]` | `GET /api/search` |
| compile | `compile` | `compile` | `POST /api/compile` |
| last result, log | `last_compile`, `get_log` | `errors`, `log [--tail N]` | `GET /api/compile/last`, `/api/log` |
| PDF text, page image | `pdf_text`, `view_pdf_page` | `pdf-text`, `pdf-page N` | `GET /api/pdf/text`, `/api/pdf/page` |
| source line to PDF and back | `source_to_pdf`, `pdf_to_source` | `where FILE LINE`, `which PAGE X Y` | `GET /api/synctex/view`, `/api/synctex/edit` |
| history, restore | `file_history`, `get_version`, `restore_version` | `history`, `version`, `restore` | `GET /api/history`, `/api/history/get`, `POST /api/history/restore` |
| what people look at | `get_presence` | `presence` | `GET /api/presence` |
| point a person to a line | `open_in_editor` | `open FILE LINE --message` | `POST /api/ui/open` |
| review notes | `list_notes`, `add_note`, `resolve_note` | `notes`, `note`, `resolve` | `GET/POST /api/notes`, `POST /api/notes/resolve` |
| change feed | `wait_for_events` | `events --since N --timeout S` | `GET /api/events` |

## The built-in assistant

smartex can also host an agent itself: a persistent Claude Code session on the server, used from the
Assistant tab. If you are a different agent working in the same project, you will see its changes
attributed to `agent:claude` on the event feed. You can message it too (`POST /api/agent/message`,
`smartex ask`) and read its conversation (`GET /api/agent/transcript`), for example to delegate a long
compile-and-fix loop while you do something else.

## Errors

Errors are JSON objects with an `error` message and an HTTP status: 400 bad request, 403 forbidden
(token, path outside the project, file type), 404 not found, 409 conflict (stale `base_sha`, file
exists, or project name taken), 422 edit text not found or not unique, 501 optional tool missing on the server.
