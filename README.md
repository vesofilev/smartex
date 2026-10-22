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
- Compile with latexmk and see the paper next to the source; click an error to jump to its line
- After each compile the paper scrolls to where your cursor is (SyncTeX); Cmd/Ctrl-J does it on demand,
  and clicking a spot on the paper jumps to its source line
- Two themes: a blackboard and a light paper theme
- See agent changes as they happen: open files reload, review notes appear in the margin

For agents (API, CLI, MCP):
- Read files or line ranges, search the project, change files by exact find-and-replace
- Compile and get structured errors, warnings and overfull boxes; read the log
- See the result: PDF text, page images, and SyncTeX lookup from a source line to a PDF page and back
- Work with people: see what they are looking at, open a location in their editor, leave review notes
  instead of editing their text, and wait on a change feed instead of polling
- Every write is checked against the version you read, and the previous version is kept

## The assistant

The **Assistant** tab next to the PDF is a conversation with Claude that runs on the server, not on your
computer. smartex starts one long-running Claude Code process per project in headless streaming mode:
- **It stays awake** between messages, with the whole conversation in memory (about 270 MB on the server).
- **It resumes**: if it is stopped or the server restarts, the next message continues the same saved
  conversation.
- **It sees what you see**: each message carries the file, line and selected text you are looking at, so
  "rephrase the selected sentence" or "why does this equation fail?" just work.
- **Its changes go through smartex**: Claude Code's own Edit and Write tools are disabled, so every change
  is attributed to `agent:claude`, versioned, and appears live in your editor. By default it comments on
  your text with review notes instead of rewriting it.
- **You can watch it work**: the status line names the step it is on ("Working: compile (step 3)"); click
  it, or any "N steps" block in the conversation, to see each step with its input, result and any thinking.
- **Its tools**: the smartex MCP tools, read-only file access, and the `fr` command from
  [frontier](https://github.com/vesofilev/frontier) for the literature, when installed.

Choose the **model** (Opus, Sonnet, Haiku, Fable, or Claude Code's default) and the **effort** (low to max)
from the menus in the Assistant tab; a change restarts the assistant and keeps the conversation. Formulas
in the conversation are typeset with KaTeX.

It needs Claude Code installed and logged in on the server (`SMARTEX_CLAUDE` sets the path if `claude` is
not on `PATH`). It uses that login, that is, the Claude subscription of the account the server runs
under; an `ANTHROPIC_API_KEY` in the environment is ignored.

**Use it only for yourself.** Anthropic's terms allow a subscription to be used through Claude Code by its
owner, but not to serve other people's requests. If several people use one smartex server, set
`SMARTEX_AGENT_API_KEY` to an Anthropic API key; the assistant then runs on that key instead.

From scripts: `./smartex ask "check that every \\label is referenced"` sends a message and prints the
reply; `./smartex transcript`, `./smartex agent-stop` and `./smartex agent-reset` do the rest.

## Requirements

- Python 3.8 or newer on the machine with the LaTeX project
- TeX Live with `latexmk` (set `SMARTEX_TEXBIN` if it is not on `PATH`)
- Optional: `pdftotext` and `pdftoppm` from poppler, for PDF text and page images
- Optional: Claude Code, logged in, for the assistant

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

## Behind a reverse proxy (no tunnel)

If an nginx you control can reach the server, smartex can be served at a sub-path such as
`https://example.org/smartex/`, with no SSH tunnel. smartex then opens a second listener that accepts
connections only from the proxy's address, and the browser signs in once with the access token. The token
goes in a form, never in an address, so it does not end up in logs; the sign-in sets an HttpOnly,
SameSite=Strict cookie, marked Secure over https and scoped to the sub-path.

Put the settings in `~/.smartex.env` on the server, so every `./smartex.sh start` uses them:

```sh
SMARTEX_PUBLIC=0.0.0.0:5005            # the port the proxy connects to
SMARTEX_ALLOW_FROM="192.0.2.10"        # the address the proxy's connections arrive from
SMARTEX_PUBLIC_URL=https://example.org/smartex/
```

and add this to the nginx server block:

```nginx
location /smartex/ {
    proxy_set_header Host               $host;
    proxy_set_header X-Real-IP          $remote_addr;
    proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto  $scheme;
    proxy_set_header X-Forwarded-Prefix /smartex;
    proxy_read_timeout                  300;    # compiles and the change feed take a while
    proxy_buffering                     off;
    client_max_body_size                20m;    # saving a large file
    proxy_pass                          http://SERVER:5005/;
}
location = /smartex {
    return 301 /smartex/;
}
```

The loopback listener, the tunnel, the CLI, the MCP server and the assistant keep working as before.
Anyone who can reach the sub-path sees the sign-in page, so keep the token secret; it gives full access
to the project and to the assistant.

## Security

The server listens on 127.0.0.1, so it is reachable from other computers only through SSH, unless you
add the reverse-proxy listener above, which accepts only the addresses you name. Every request must carry
the access token stored in `~/.smartex_token` (mode 0600), because other users of the same machine can
reach its local ports. A browser signed in with the cookie can change things only from the smartex page
itself: every POST needs the X-Client header and a same-origin Origin. Files outside the project folder cannot be read or written,
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
