---
name: mcpc
description: Discover and call tools on MCP servers through the `mcpc` command line. Use when a task needs an external tool, API or data source that may be exposed by an MCP server, or when the user mentions MCP.
---

# mcpc: use MCP servers from the shell

`mcpc` talks to the user's configured MCP servers. Tools are addressed as `server/tool`.
Global options (`--json`, `--workspace`, `--refresh`, `--timeout`, `--no-daemon`, `-v`, `-A`/`--agent`) go **before** the command: `mcpc --json call ...`.

## Workflow

1. **Find what you need.** Describe what you need in plain words:
   `mcpc search "create a github issue"`. By default, search ranks across tools, servers, skills, and plugins (`-t all`). Use `-t mcp` (tools and servers), `-t skill` (skills), `-t plugin` (plugins), or `-t tool` (only tools).
   To browse instead: `mcpc list` (servers), `mcpc show <server>` (its tools).
2. **Read the tool before calling it.** `mcpc show <server>/<tool>` prints the parameters, which are required, defaults, and ends with an example `call` line. Do not guess parameter names.
3. **Call it.**
   - Simple values: `mcpc call <server>/<tool> -a query=dune -a limit=5`. A value is sent as JSON when it parses (`limit=5` is the integer 5), otherwise as a string.
   - Complex input (objects, arrays, long text): write a JSON file and pass it.
     bash: `mcpc call <server>/<tool> --args @args.json`
     PowerShell: `mcpc call <server>/<tool> --args '@args.json'` (quote it; a bare `@name` is splatting)
   - Or pipe JSON on stdin: `--args -`.
   - Arguments are validated against the tool schema before sending; the error names the bad field.
4. **Check the exit code**, then read stdout.

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | Use the output. |
| 1 | The tool ran and reported an error (message is on stdout) | Read the message, fix the input, retry. |
| 2 | Bad usage: wrong arguments, unknown server or tool, invalid JSON | Read the stderr line (it suggests close names), fix the command. Run `mcpc show <server>/<tool>`. |
| 3 | Cannot connect, timeout, command not found | Retry once. Then `mcpc check <server>` and tell the user; the server log is `~/.mcpc/logs/<server>.log`. |
| 4 | Protocol error, or the tool needs a feature mcpc lacks (elicitation, sampling) | Do not retry. Report it to the user. |
| 5 | Workspace not trusted | Stop. Ask the user to run `mcpc trust` in the workspace themselves. **Never** try to trust a workspace, set `MCPC_TRUST_ALL`, or edit `~/.mcpc/trust.json`. |

## Output

- Default output is plain text. Add `--json` when you parse the result: `mcpc --json call ...` prints `{"server","tool","is_error","content":[...],"structured_content":{...}}`. Prefer `structured_content` when present.
- With `--json`, errors are also printed on stdout as `{"error": {"code","kind","message","server"}}`.
- Images, audio and binary data are not printed. mcpc saves them and prints the file path (`--json`: a `path` field). Open that file if you need it.
- Large output: add `--max-output 4000` to `call` or `read` to cap the text.

## Other commands

- `mcpc resources <server>`, `mcpc read <server> <uri>`: server data. `mcpc prompts <server>`, `mcpc prompt <server>/<name> -a k=v`: prompt templates.
- `mcpc --refresh show <server>` or `mcpc refresh <server>`: reload a stale tool list (the cache lasts 24 hours).
- `mcpc list --full`: transport, status (`ok`, `error`, `untrusted`, `disabled`, `not cached`), tool counts.

## Coding agents integration

mcpc discovers MCP servers, plugins, and skills from coding agents (Claude Code, Google Antigravity CLI).

- `-A, --agent <all|claude-code|antigravity|none>`: Load servers defined across agents (e.g. `mcpc -A all list`, `mcpc -A all search "..."`).
- `mcpc agents`: Inspect status, configs, and asset counts of detected coding agents.
- `mcpc plugins [NAME] [--agent AGENT] [--enabled] [--raw]`: Inspect plugins.
- `mcpc skills [NAME] [--agent AGENT] [--plugin PLUGIN] [--full] [--raw]`: List skills or print complete SKILL.md content.
- Deduplication: By default, skills, plugins, and servers shared across agents are deduplicated and merged with combined agent tags and source paths. Use `--raw` to view raw entries.

## Rules

- Search and show first; call second. Tool names differ between servers (two servers can both have a `search` tool), so always use `server/tool`.
- Do not edit `~/.mcpc/mcp.json` or `.mcpc/mcp.json` unless the user asks. Never print or copy header or token values.
- Calls can have side effects (`add_review`, `create_issue`). Do not repeat a call that may have succeeded unless the user wants it.
- Run commands non-interactively. `mcpc` never prompts except for `mcpc trust`.
