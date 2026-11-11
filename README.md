# mcpc

`mcpc` is a command-line client for [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers. It reads server definitions from its own config files, connects over stdio, Streamable HTTP or SSE, and exposes tools, resources and prompts as short commands. It is built to be driven by AI agents as well as people: output is plain text by default, `--json` is available everywhere, exit codes are stable, and a background daemon keeps server connections open between calls.

## Install

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install mcpc
```

Or install directly from GitHub:

```bash
uv tool install git+https://github.com/meossistant/mcpc.git
```

To upgrade:

```bash
uv tool upgrade mcpc
```

## Quick start

1. Create `~/.mcpc/mcp.json` (on Windows: `C:\Users\<you>\.mcpc\mcp.json`):

   ```json
   {
     "mcpServers": {
       "bookshop": {
         "command": "python",
         "args": ["tests/fixtures/bookshop_server.py"],
         "description": "Book catalog: search, lookup and reviews"
       }
     }
   }
   ```

2. Use it:

   ```
   $ mcpc list
   bookshop  Book catalog: search, lookup and reviews

   $ mcpc search "find a book by author"
   bookshop/search_books  Search the catalog by title or author.
   bookshop/lookup_book   Look up a book by its exact title.
   bookshop/add_review    Add a customer review to a book.
   bookshop/crash         Exit the server process abruptly (for crash tests).
   bookshop/cover_image   Return a tiny PNG cover image.

   $ mcpc show bookshop
   bookshop: Book catalog: search, lookup and reviews
   tools (5):
     search_books: Search the catalog by title or author.
     lookup_book: Look up a book by its exact title.
     add_review: Add a customer review to a book.
     cover_image: Return a tiny PNG cover image.
     crash: Exit the server process abruptly (for crash tests).

   $ mcpc show bookshop/search_books
   bookshop/search_books  (Search the catalog)
     Search the catalog by title or author.

     Returns a short summary of matching books.
   parameters:
     query  string  required
     limit  integer  default: 10
   output: object {result: string}
   example: mcpc call bookshop/search_books -a query=<string>

   $ mcpc call bookshop/search_books -a query=dune -a limit=5
   Found 3 books matching 'dune' (showing up to 5).
   ```

`list` reads the catalog cache and never starts a server. `search` and `show` fill the cache on first use.

## Configuration

### File locations

| Scope | Path | Override |
| --- | --- | --- |
| Global | `~/.mcpc/mcp.json` | `MCPC_HOME` moves the whole `~/.mcpc` directory; `--config PATH` replaces just this file |
| Workspace | `<workspace>/.mcpc/mcp.json` | `--workspace DIR` |

The workspace root is the first directory, walking up from the current one, that contains `.mcpc/`. If none does, it is the first directory containing `.git`, and otherwise the current directory.

`mcpc config path` prints the files in use:

```
$ mcpc config path
global: ~/.mcpc/mcp.json (exists)
workspace: ~/work/myrepo/.mcpc/mcp.json (missing)
workspace root: ~/work/myrepo
```

### Format

```jsonc
{
  "mcpServers": {
    // stdio: mcpc starts the process
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${workspaceFolder}"],
      "env": { "LOG_LEVEL": "warn" },
      "cwd": "${workspaceFolder}",
      "timeout": 60,
      "tags": ["files"]
    },
    // Streamable HTTP (the default for "url")
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer ${env:GITHUB_TOKEN}" },
      "description": "GitHub issues, PRs and repos"
    },
    // legacy SSE
    "legacy-sse": { "url": "http://localhost:9000/sse", "transport": "sse" }
  },
  "settings": {
    "cacheTtlHours": 24,
    "connectTimeout": 30,
    "requestTimeout": 300,
    "daemon": { "enabled": true, "serverIdleMinutes": 15, "daemonIdleMinutes": 60 }
  }
}
```

(The files are plain JSON. Remove the comments.)

Each entry needs exactly one of `command` or `url`. Server names may contain letters, digits, `_`, `.` and `-`. An invalid entry produces a warning on stderr and is skipped; the other servers still load. `type` is accepted as an alias for `transport`, and `http` as an alias for `streamable-http`.

Fields:

| Field | Applies to | Meaning |
| --- | --- | --- |
| `command`, `args`, `env`, `cwd` | stdio | Process to start. `env` is added to a restricted default environment. This is how API keys reach a server. |
| `url`, `headers`, `transport` | HTTP | Endpoint, static headers, and `streamable-http` (default) or `sse`. |
| `description` | all | Shown in `mcpc list` and used by search. Without it mcpc falls back to the first sentence of the server's `instructions`, then its title, then `(no description)`. |
| `tags` | all | Extra search terms. |
| `timeout` | all | Seconds per request for this server (default: `settings.requestTimeout`). |
| `mode` | all | `auto` (default) negotiates the protocol version. `legacy` forces the older `initialize` handshake. |
| `disabled` | all | `true` hides the server from `list` and refuses to use it. `mcpc list --all` shows it. |

### Variables

These expand in `command`, `args`, `env`, `cwd`, `url` and `headers`:

| Syntax | Value |
| --- | --- |
| `${env:NAME}` or `${NAME}` | Environment variable of the shell that runs `mcpc`. An unset variable is an error. |
| `${env:NAME:-default}` | Same, with a fallback. |
| `${workspaceFolder}` | The detected (or `--workspace`) workspace root. |
| `${userHome}` | The user's home directory. |
| `$${` | A literal `${`. |

Expansion happens just before connecting. Expanded values are never written to disk, logged or printed. `mcpc config` and `mcpc list --full` show the templates as written and replace header and env values that are not a single `${...}` with `***`.

### Workspace overrides

Servers from the global file load first, then the workspace file. A workspace entry with the same name replaces the global one (`list --full` marks it as overriding). A workspace entry containing only `"disabled": true` switches the global server of that name off for this workspace:

```json
{ "mcpServers": { "github": { "disabled": true } } }
```

### Settings

Read from the global file only (a `settings` block in a workspace file is ignored with a warning).

| Key | Default | Meaning |
| --- | --- | --- |
| `cacheTtlHours` | `24` | How long a cached catalog stays fresh. |
| `connectTimeout` | `30` | Seconds allowed to connect and handshake. |
| `requestTimeout` | `300` | Default seconds per request. A server's `timeout` or `--timeout` overrides it. |
| `daemon.enabled` | `true` | Use the background daemon. |
| `daemon.serverIdleMinutes` | `15` | Close a server session after this long without requests. |
| `daemon.daemonIdleMinutes` | `60` | Exit the daemon after this long with no live servers and no requests. |

## Workspace trust

A workspace config file comes from a repository, and a stdio entry in it is a command line. If you clone a repository and an agent runs `mcpc search` there, mcpc would start whatever that file says, with your permissions. An HTTP entry that puts `${env:SECRET}` in a header would send your secret to the URL.

So workspace servers that run a command (every stdio server) or reference environment variables in `url` or `headers` are untrusted until you approve them. Global config is never affected. Run this in the workspace:

```
$ mcpc trust
The workspace ~/work/myrepo defines MCP servers that run commands or send your environment variables:
  notes: python tests/fixtures/weather_server.py
Trust this workspace configuration? [y/N]:
```

`mcpc trust` needs an interactive terminal and refuses (exit code 2) when stdin is a pipe or file, so an agent running commands non-interactively cannot approve on your behalf. Approval stores the sha256 of `.mcpc/mcp.json` in `~/.mcpc/trust.json`. If the file changes, for example after a `git pull`, trust is revoked and you approve again. `mcpc untrust` revokes it manually.

Until then the server shows as `untrusted` in `mcpc list`, and connecting fails with exit code 5:

```
$ mcpc call notes/forecast -a city=Hue
mcpc: notes: run "mcpc trust" in ~/work/myrepo to allow its MCP servers
```

For CI or disposable containers, `MCPC_TRUST_ALL=1` turns the check off.

## Commands

Global options go **before** the command: `mcpc --json call ...`, not `mcpc call --json ...`.

| Option | Meaning |
| --- | --- |
| `--json` | Machine-readable output. |
| `-w, --workspace DIR` | Workspace root (default: detected from the current directory). |
| `--config PATH` | Global config file. |
| `--scope all\|global\|workspace` | Which servers to consider (default `all`). |
| `-A, --agent none\|all\|claude-code\|antigravity` | Coding agent integration mode (default `none`, or set in `settings.agentMode`). |
| `--refresh` | Ignore the catalog cache and reconnect. |
| `--timeout SECONDS` | Request timeout for this run. |
| `--no-daemon` | Connect directly without the daemon. |
| `-v, --verbose` | Diagnostics on stderr. |
| `--version` | Print the version. |

| Command | What it does |
| --- | --- |
| `mcpc list [--full] [--all]` | Servers with descriptions. `--full` adds scope, source file, transport, target, status (`ok`, `error`, `untrusted`, `disabled`, `not cached`), protocol version, counts and cache age. `--all` includes disabled servers. Reads the cache; connects only with `--refresh`. |
| `mcpc show <server> [--full]` | The server's tools with one-line descriptions. `--full` adds parameters, output schemas, resources and prompts. |
| `mcpc show <server>/<tool>` | Full detail of one tool, ending with an example `call` line. |
| `mcpc search "<query>" [-n 10] [-s SERVER] [-t all\|mcp\|tool\|server\|skill\|plugin]` | BM25 search across tools, servers, skills, and plugins (default: all). |
| `mcpc call <server>/<tool> [-a k=v]... [--args JSON\|@file\|-]` | Call a tool. Also accepts `mcpc call <server> <tool>`. |
| `mcpc resources <server>` | List resources and resource templates. |
| `mcpc read <server> <uri>` | Read one resource. |
| `mcpc prompts <server>` | List prompts. |
| `mcpc prompt <server>/<name> [-a k=v]...` | Render a prompt. |
| `mcpc refresh [server...]` | Reconnect and rebuild the cache, in parallel. |
| `mcpc check [server...]` | Connect and report protocol version, latency and capabilities. Non-zero exit if any server fails. |
| `mcpc agents` | Show status and asset breakdown of supported coding agents (Claude Code, Antigravity CLI). |
| `mcpc plugins [NAME] [--agent AGENT] [--enabled] [--raw]` | List or inspect plugins from coding agents. Cross-agent duplicates merged by default (`--raw` for all). |
| `mcpc skills [NAME] [--agent AGENT] [--plugin PLUGIN] [--full] [--raw]` | List or view skills (`SKILL.md`) from coding agents and plugins. Cross-agent duplicates merged by default (`--raw` for all). |
| `mcpc config` / `mcpc config path` | Merged configuration with each server's source / file locations. |
| `mcpc trust [-y]` / `mcpc untrust` | Approve or revoke the workspace's servers. |
| `mcpc daemon start\|stop\|status\|restart` | Control the daemon. |
| `mcpc install-instruction [--skill]` | Print steps that guide an AI agent through installing the mcpc skill. `--skill` prints the raw `SKILL.md`. |

`call`, `read` also take `--max-output N` (truncate text to N characters) and `--out-dir DIR` (where binary content is saved; default `~/.mcpc/tmp`).

### Passing arguments to `call`

- `-a key=value` is repeatable. A value is parsed as JSON when it is valid, otherwise kept as a string: `-a limit=5` sends the integer 5, `-a query=dune` the string `dune`. If the tool's schema wants a string, `-a title=123` is sent as `"123"`.
- `--args '<json>'` takes a JSON object. `--args '@file.json'` reads it from a file. `--args -` reads it from stdin. `-a` pairs are merged over `--args`.
- Arguments are checked against the tool's schema before sending. `--no-validate` skips that.

In PowerShell, quote `@file.json` (`--args '@args.json'`), because a bare `@name` is parsed as splatting.

```
$ mcpc call bookshop/search_books -a limit=5
mcpc: bookshop: invalid arguments for bookshop/search_books:
  - query: 'query' is a required property
```

### Coding agents integration & deduplication

`mcpc` can discover and consume MCP servers, plugins, and skills from coding agents (Claude Code, Google Antigravity CLI):

- **Activate agent mode**: Pass `-A all` (or `-A claude-code`, `-A antigravity`) or configure `"agentMode": "all"` in `settings` of `~/.mcpc/mcp.json`.
- **Cross-agent deduplication**:
  - **Skills**: When the same skill exists across multiple agents (e.g. `nanopi`), `mcpc` automatically deduplicates them by normalized name, merging their supported agents (`agents: list[str]`) and source file paths (`sources: list[str]`).
  - **Plugins**: Duplicate plugins merge their skill sets, MCP server definitions, and agent tags.
  - **MCP Servers**: Servers sharing the same endpoint URL or stdio command/args merge tags (`agent:claude-code`, `agent:antigravity`) to prevent duplicate connections.
- **Inspect raw items**: Use `--raw` on `mcpc skills` or `mcpc plugins` to inspect all unmerged instances across agent directories.

## Output and exit codes

Output is plain text by default. Errors are one line on stderr: `mcpc: <server>: <message>`. Server stderr never reaches stdout. When a tool itself reports an error (`is_error`), its message is printed on stdout and the exit code is 1:

```
$ mcpc call bookshop/lookup_book -a title=Foo
error: Error executing tool lookup_book: No book titled 'Foo' in the catalog.
```

With `--json`, every command prints one JSON document on stdout. A call looks like this:

```
$ mcpc --json call bookshop/lookup_book -a title=Dune
{
  "server": "bookshop",
  "tool": "lookup_book",
  "is_error": false,
  "content": [{"type": "text", "text": "{\n  \"title\": \"Dune\", ...}"}],
  "structured_content": {"title": "Dune", "author": "Frank Herbert", "year": 1965}
}
```

Failures also print `{"error": {"code", "kind", "message", "server"}}` on stdout, in addition to the stderr line:

```
$ mcpc --json call bookshp/x
{"error": {"code": 2, "kind": "not_found", "message": "unknown server 'bookshp'. Did you mean: bookshop?"}}
```

Images, audio and binary resources are not printed. They are saved under `~/.mcpc/tmp/` (or `--out-dir`) and the path is shown:

```
$ mcpc call bookshop/cover_image
[image saved: ~/.mcpc/tmp/ab983596c8c5468cb2d57a797dbb0b05.png (image/png, 70 bytes)]
```

With `--json` such a block is `{"type": "image", "mimeType": "image/png", "path": "...", "size": 70}`.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | The tool ran and reported an error (`is_error`), or `mcpc trust` was declined |
| 2 | Usage or config error: bad arguments, unknown server or tool, invalid JSON, schema validation failed, `mcpc trust` without a terminal |
| 3 | Connection or transport failure, timeout, command not found |
| 4 | Protocol error, invalid server response, or a feature mcpc does not support (sampling, elicitation) |
| 5 | Workspace not trusted |

`refresh` and `check` exit with the code of the first server that failed.

## Daemon

Starting an `npx` or `uvx` server often takes a second or more. The daemon is a per-user background process that keeps server sessions open, so repeated calls skip process start-up and the handshake.

- It starts automatically on the first command that needs a connection. `--no-daemon` or `"daemon": {"enabled": false}` skips it.
- It closes a server after 15 idle minutes and exits after 60 minutes with nothing to do (see Settings).
- The CLI still does all config loading, variable expansion and trust checks, and sends the daemon the resolved server definition. The daemon never reads config files. Resolved secrets live only in its memory.
- It listens on `127.0.0.1` on a random port. Every request carries a token from `~/.mcpc/daemon.json`.
- After `uv tool upgrade mcpc`, the next command notices the version change and replaces the daemon.
- If the daemon cannot be reached or started within a few seconds, mcpc prints a warning on stderr and connects directly.

```
$ mcpc daemon status
daemon running (pid 29232, version 0.1.0, up 47s)
servers:
  weather   stdio  2026-07-28  idle 6s
  bookshop  stdio  2026-07-28  idle 4s

$ mcpc daemon stop
daemon stopped
```

`mcpc daemon start`, `stop`, `status` and `restart` do what they say. The daemon log is `~/.mcpc/logs/daemon.log`. It records request outcomes and timings, never server definitions, headers or tokens. Each `MCPC_HOME` has its own daemon.

## Files in `~/.mcpc`

| Path | Content |
| --- | --- |
| `mcp.json` | Global config. |
| `cache/<server>-<hash>.json` | Catalog snapshot (tools, resources, prompts, protocol version). The hash comes from the unexpanded server definition, so editing a server invalidates its entry. Safe to delete. |
| `logs/<server>.log` | stderr of that stdio server. |
| `logs/daemon.log` | Daemon log. |
| `trust.json` | Approved workspaces and the hash of their config file. |
| `daemon.json` | Daemon pid, port and token. Treat as a secret. |
| `daemon.lock` | Prevents two CLIs from starting two daemons. |
| `tmp/` | Saved images, audio and binary content. |

## Coding Agents Integration

`mcpc` can discover and interoperate with plugins, skills, and MCP servers from other coding agents.

### Supported Agents

- **Claude Code**: Reads MCP servers from `~/.claude.json`, `.claude/mcp.json`, and workspace `.mcp.json`. Discovers skills from `~/.claude/skills/` and `~/.agents/skills/`.
- **Antigravity CLI**: Reads MCP servers from `~/.gemini/config/mcp_config.json`, enabled plugins in `~/.gemini/config/plugins/`, and workspace configs. Discovers all plugin metadata and skills.

### Usage

```bash
# Overview of detected agents on the system
mcpc agents

# List all discovered plugins from coding agents
mcpc plugins
mcpc plugins cloudflare   # Inspect specific plugin detail

# List and view skills
mcpc skills
mcpc skills nanopi        # View full skill document and instructions

# Ingest and call MCP servers from agents
mcpc --agent all list
mcpc --agent antigravity show cloudflare
mcpc --agent claude-code list
```

To enable agent server ingestion permanently without passing `--agent`, add to `~/.mcpc/mcp.json`:

```json
{
  "settings": {
    "agentMode": "all"
  }
}
```

## Troubleshooting

- **A server fails or behaves oddly.** Read `~/.mcpc/logs/<server>.log`. Server stderr is written there, not to your terminal.
- **Is the server reachable?** `mcpc check <server>` connects and prints protocol version, latency and capabilities. A start-up failure shows up as exit code 3:

  ```
  $ mcpc check
  x  error  command not found: [WinError 2] The system cannot find the file specified
  ```

- **Stale list of tools.** `mcpc refresh <server>`, or add `--refresh` to any command. The cache is otherwise reused for `cacheTtlHours`.
- **More detail.** `mcpc -v <command>` prints diagnostics on stderr, including tracebacks for unexpected errors.
- **`unknown server` / server missing from `list`.** Run `mcpc config` to see which files were loaded and why an entry was skipped (warnings are listed at the end). Check `--scope` and `disabled`.
- **`run "mcpc trust"` (exit 5).** See Workspace trust.
- **Suspected daemon problem.** Retry with `--no-daemon`, or run `mcpc daemon restart`.
- **`environment variable X is not set`.** Variables expand from the shell that runs `mcpc`. Set it there, or use `${env:X:-default}`.

## Development

```
uv sync
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m ruff format src tests
.venv\Scripts\python -m pyright
```

(On Linux and macOS use `.venv/bin/python`.) Tests use fixture servers in `tests/fixtures/` and a temporary `MCPC_HOME`, so they never touch `~/.mcpc`.

The design and the reasoning behind it are in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). For people integrating `mcpc` into an AI agent, see [docs/AGENT_USAGE.md](docs/AGENT_USAGE.md). The agent skill ships inside the package ([src/mcpc/skill/SKILL.md](src/mcpc/skill/SKILL.md)); to install it, tell your agent to run `mcpc install-instruction` and follow the output.
