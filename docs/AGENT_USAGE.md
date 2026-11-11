# Using mcpc from an AI agent

This page is for the person who connects an AI agent (a coding assistant, a script-driven LLM loop, a CI bot) to `mcpc`. The agent needs only a shell tool. `mcpc` does the MCP work: it starts or connects to the servers, caches their tool lists, and returns results as text or JSON.

For the agent-facing instructions, install the skill (below) or paste the prompt snippet at the end of this page.

## Install the skill

Tell the agent: "Run `mcpc install-instruction` and follow the instructions." The command prints a short checklist for the agent: check that `mcpc` runs, pick the skills directory (e.g. `~/.agents/skills/mcpc/SKILL.md`), write the file from `mcpc install-instruction --skill`, verify it, and report back. The command changes nothing itself. The skill text is read from the installed package, so it always matches the installed `mcpc` version. After `uv tool upgrade mcpc`, run it again to refresh the skill.

## Why a CLI instead of loading MCP servers into the agent

- **Small context.** The agent does not receive every tool schema up front. It searches (`mcpc search`), reads one tool (`mcpc show server/tool`), and calls it. Hundreds of tools cost nothing until used.
- **One config for every agent.** Servers are defined once in `~/.mcpc/mcp.json`. Any agent that can run a command can use them.
- **Predictable behavior.** Plain text by default, `--json` on every command, stable exit codes, server stderr kept out of stdout.
- **Fast repeat calls.** A background daemon keeps server connections open, so the tenth call does not pay for process start-up.

## Setup

1. Install `mcpc` (see the [README](../README.md#install)) and check that `mcpc --version` works in the shell the agent uses.
2. Define servers in `~/.mcpc/mcp.json`. Give each one a `description`; search and `mcpc list` use it.
3. Put secrets in environment variables and reference them as `${env:NAME}` in `headers` or `env`. They are expanded when mcpc connects and are never printed or stored.
4. Check: `mcpc check` connects to every server and reports failures with exit code 3 or 4.
5. Give the agent the skill file or the prompt snippet below.

## The workflow the agent follows

```
mcpc search "find a book by author"       # 1. find candidates, ranked
mcpc show bookshop/search_books           # 2. read parameters and the example call line
mcpc call bookshop/search_books -a query=dune -a limit=5   # 3. call
```

Step 2 is what keeps the agent from inventing parameter names. It prints each parameter with its type, `required` or `default`, and an `example:` line to copy.

For structured or long input the agent writes a JSON file and passes it with `--args @file.json` (PowerShell: `--args '@file.json'`), or pipes JSON with `--args -`. Simple values use repeatable `-a key=value`.

Global options go before the command: `mcpc --json call ...`.

## Exit codes

| Code | Meaning | Agent behavior |
| --- | --- | --- |
| 0 | Success | Use the output. |
| 1 | Tool reported an error (message on stdout) | Fix input and retry, or report. |
| 2 | Usage or config error: wrong arguments, unknown server or tool | Fix the command from the stderr message. |
| 3 | Connection failure, timeout, command not found | Retry once, then run `mcpc check` and report. |
| 4 | Protocol error, or a feature mcpc lacks (elicitation, sampling) | Do not retry. Report. |
| 5 | Workspace not trusted | Stop and ask the user to run `mcpc trust`. |

With `--json`, a failure also prints `{"error": {"code", "kind", "message", "server"}}` on stdout, so an agent can branch on `kind` (`usage`, `not_found`, `validation`, `connection`, `timeout`, `protocol`, `unsupported`, `untrusted`).

## Output handling

- Text output is the tool's own text blocks, one per block. If a tool returns only structured data, mcpc prints it as indented JSON.
- `mcpc --json call ...` returns `server`, `tool`, `is_error`, `content` and `structured_content`. Parse `structured_content` when it exists.
- Images, audio and binary resources are saved to `~/.mcpc/tmp/` (or `--out-dir DIR`) and the path is printed. The agent opens the file if it needs the data.
- `--max-output N` cuts text at N characters and appends a note. Use it when a tool can return very large results.

## Security model

- **Workspace trust.** A repository can ship `.mcpc/mcp.json`, and a stdio entry there is a command line. mcpc refuses to start workspace servers that run commands, or that send environment variables in `url` or `headers`, until a person runs `mcpc trust` in that workspace. The server shows as `untrusted` in `mcpc list` and calls exit with code 5. Trust is tied to a hash of the file, so a changed file needs approval again.
- **The agent cannot approve.** `mcpc trust` requires an interactive terminal and exits with code 2 when stdin is a pipe or file. Tell the agent in its instructions never to work around exit code 5. It must not set `MCPC_TRUST_ALL=1` (meant for CI and disposable containers) or edit `~/.mcpc/trust.json`.
- **Global config is yours.** Servers in `~/.mcpc/mcp.json` need no approval, so review what you put there.
- **Secrets.** Header and env values written as `${env:NAME}` stay in the process environment. `mcpc config` and `mcpc list --full` redact other values.
- **Side effects.** mcpc cannot tell a read-only tool from a destructive one. If your agent framework has command approval, consider requiring it for `mcpc call` and allowing `search`, `show`, `list`, `check`, `resources` and `prompts` without asking.

## Operating notes

- The tool list is cached for 24 hours (`settings.cacheTtlHours`). After a server adds tools, run `mcpc refresh <server>`.
- `mcpc list` and `mcpc search` start no server when the cache is warm. The first `search` or `show` on a new server connects once to fill the cache.
- Several agents can share one daemon. It runs per user and per `MCPC_HOME`, closes idle server sessions after 15 minutes, and exits after 60 idle minutes. `mcpc --no-daemon <command>` bypasses it, and `mcpc daemon restart` recovers from a stuck one.
- To give an agent a separate set of servers, point it at another directory with the environment variable `MCPC_HOME`, or use a project-level `.mcpc/mcp.json` (which needs `mcpc trust` for stdio servers).
- Server problems show up in `~/.mcpc/logs/<server>.log`. `mcpc -v <command>` adds diagnostics on stderr.

## Prompt snippet

Paste this into a system prompt, `AGENTS.md`, or equivalent. It is a short form of the skill (`mcpc install-instruction --skill` prints the full text).

```
You can use MCP servers through the `mcpc` command line tool. Tools are named `server/tool`.

Workflow:
1. Find a tool: `mcpc search "<what you need>"`. Browse with `mcpc list` and `mcpc show <server>`.
2. Before calling, read it: `mcpc show <server>/<tool>`. Use the parameter names and types it shows.
3. Call it: `mcpc call <server>/<tool> -a key=value -a other=value`. For objects, arrays or long text,
   write a JSON file and use `--args @file.json` (in PowerShell: `--args '@file.json'`) or `--args -` with JSON on stdin.
4. Global options such as `--json` go before the command: `mcpc --json call ...`. Use `--json` when you parse output.

Exit codes: 0 ok. 1 the tool reported an error (message on stdout): fix the input. 2 bad arguments or unknown
server or tool: fix the command using the message. 3 connection failure or timeout: retry once, then run
`mcpc check <server>` and tell the user. 4 protocol error: do not retry, tell the user. 5 workspace not trusted:
stop and ask the user to run `mcpc trust` themselves. Never run `mcpc trust`, set MCPC_TRUST_ALL, or edit trust files.

Images and other binary results are saved to files; mcpc prints the path. Cap large text with `--max-output N`.
Tool calls can have side effects, so do not repeat a call that may have succeeded unless asked.
```
