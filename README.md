# dangerzone-mcp

A Python example of a self-modifying MCP server, built on the official MCP
Python SDK. The agent connected to it can write a new tool, call it, fix it,
and find it still there in the next session.

The server has three permanent tools: `add_tool`, `edit_tool`, and
`remove_tool`. Everything else in the catalog is Python that arrived at
runtime, usually written by the agent, saved in a JSON file in your project.

The name is a reminder that this runs model-written Python on your machine with
your permissions. Read [Trust](#trust) before pointing it at anything you care
about.

## What it looks like

![Claude Code creating a square tool, editing it to cube, and calling it again after a restart](docs/demo.gif)

[Register the server](#register), then ask for a tool:

> Use dangerzone to create a tool that squares a number. Call it with 7, then
> edit it to cube numbers and call it again.

The agent calls `add_tool` with Python source and an input schema. The server
saves the definition and notifies the client that its tool list changed. Calling
`square` with 7 returns 49; after `edit_tool` changes it to cube numbers, the same
call returns 343. Restarting the server restores the edited tool. `remove_tool`
takes it out of the catalog when it is no longer needed.

Each call validates its arguments and runs the Python code in a fresh worker,
with a 30-second timeout by default (`--timeout SECONDS`). Failures return tool
errors so the session can continue.

## Register

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git,
then clone the repository and read it. The server runs on your machine with your
permissions, and there is no install path that skips that step on purpose.

```sh
git clone https://github.com/khuber/dangerzone-mcp.git
cd dangerzone-mcp && uv sync
```

### Claude Code

From the checkout:

```sh
claude mcp add --transport stdio --scope user dangerzone -- \
  uv run --project "$PWD" dangerzone-mcp
```

This makes the server available across your projects. Run `/mcp` in Claude Code
to check the connection. Each project keeps its own catalog as described under
[Persistence](#persistence). Updating is a `git pull` in the checkout followed
by a reconnect; nothing changes until you do that.

### Other MCP clients

For clients that accept `mcpServers` JSON and refresh tools after change
notifications, replace the path with your checkout:

```json
{
  "mcpServers": {
    "dangerzone": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/absolute/path/to/dangerzone-mcp",
        "dangerzone-mcp"
      ]
    }
  }
}
```

See [client compatibility](#client-compatibility) for the Codex limitation.
The [setup guide](docs/setup.md) covers registration scopes, server options,
updates, and troubleshooting.

## Persistence

Tools survive restarts in a project-local `dangerzone.tools.json`. The server
uses the containing Git root, or the starting directory outside Git. Clients
start it in the project directory; `--project-dir PATH` overrides the starting
point. Add `--no-persist` to the server command to keep tools in memory for one
process.

Keep personal catalogs out of Git, or deliberately commit reviewed tools to
share them. See the [catalog reference](docs/reference.md#catalog-file) for the
file format, recovery behavior, and [ignore entries](docs/reference.md#git-ignore-rules).

## Trust

Workers are process separation, not a sandbox. Tool code has the server user's
file, network, and process access. That is fine for tools I asked for on my
own laptop and not fine for much else. To run code you do not trust, put the
workers under a different OS user or in a container with the server package
read-only.

Persistence adds a second consideration. A repository you clone can arrive
with a `dangerzone.tools.json` already in it, written by whoever committed it,
and the agent uses the tool descriptions in that file to decide what to call.
Treat a catalog you did not write the way you would treat a Makefile from a
stranger: read the source and the descriptions before enabling it, or start
the server with `--no-persist` so it never loads.

## Client compatibility

The add-then-call workflow requires a client that refreshes its tool list after
change notifications. Codex has a reported issue where tools added after
initial discovery never become callable in the task, including in later turns:
[openai/codex#43642](https://github.com/openai/codex/issues/43642).
Codex setup examples are omitted until this workflow is verified to work there.

For implementation details and limitations, see the [reference](docs/reference.md).
For development setup and checks, see the [setup guide](docs/setup.md).

Built on the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
2.2 against the 2026-07-28 spec. [MIT licensed](LICENSE).
