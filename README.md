# smartex

A lightweight LaTeX workspace for projects that live on a remote machine, shared by people and AI agents.

People get a browser editor with a live PDF preview. Agents get the same operations through a JSON API,
a command-line tool and an MCP server. Both work on the same files with the same safety rules: every
change is attributed, versioned and announced, so a person can always see what an agent did and undo it.

The server is a single Python file with no dependencies. The browser tab and the server together use a
small fraction of the memory an IDE needs.

## What it does

For people (browser):
- Edit `.tex`, `.bib`, `.sty`, `.cls`, `.md` files with LaTeX highlighting, search and a section outline
- Compile with latexmk and see the PDF next to the source; click an error to jump to its line
- Two themes: a blackboard and a light paper theme
- See agent changes as they happen: open files reload, review notes appear in the margin

For agents (API, CLI, MCP):
- Read files or line ranges, search the project, change files by exact find-and-replace
- Compile and get structured errors, warnings and overfull boxes; read the log
- See the result: PDF text, page images, and SyncTeX lookup from a source line to a PDF page and back
- Work with people: see what they are looking at, open a location in their editor, leave review notes
  instead of editing their text, and wait on a change feed instead of polling
- Every write is checked against the version you read, and the previous version is kept

## Requirements

- Python 3.8 or newer on the machine with the LaTeX project
- TeX Live with `latexmk` (set `SMARTEX_TEXBIN` if it is not on `PATH`)
- Optional: `pdftotext` and `pdftoppm` from poppler, for PDF text and page images

## Quick start

On the machine with the project:

```sh
git clone https://github.com/vesofilev/smartex.git
cd smartex
./smartex.sh start ~/papers/my-paper     # prints the address to open
```

On your laptop, open an SSH tunnel and browse to that address:

```sh
ssh -N -L 8765:127.0.0.1:8765 you@server
# then open http://localhost:8765/?token=...   (./smartex.sh url prints it again)
```

`./smartex.sh stop` stops the server and `./smartex.sh status` shows whether it runs. Several projects
can be served at once: `./smartex.sh start ~/papers/a ~/papers/b`.

## Security

The server listens on 127.0.0.1 only, so it is reachable from other computers only through SSH. Every
request must carry the access token stored in `~/.smartex_token` (mode 0600), because other users of
the same machine can reach its local ports. Files outside the project folder cannot be read or written,
and only text formats used by LaTeX projects can be edited.

## For AI agents

Read [AGENTS.md](AGENTS.md). In short:

```sh
# Claude Code (or any MCP client): register the MCP server
claude mcp add smartex -- python3 /path/to/smartex/smartex_mcp.py --client agent:claude-code

# or use the command-line tool
./smartex help                     # the full endpoint catalogue
./smartex cat main.tex --numbered
./smartex edit main.tex --old 'vaccum' --new 'vacuum'
./smartex compile && ./smartex pdf-page 1 -o page1.png
```

The HTTP API describes itself at `GET /api/help`.

## Files

| file | purpose |
|---|---|
| `server.py` | the server: HTTP API, compilation, history, notes, event feed |
| `index.html` | the browser editor |
| `smartex` | command-line client |
| `smartex_mcp.py` | MCP server (stdio) for AI agents |
| `smartex_client.py` | Python client shared by the CLI and the MCP server |
| `smartex.sh` | start, stop and inspect the server |

Per-project data (version history and review notes) is kept in a hidden `.smartex/` folder inside each
project.
