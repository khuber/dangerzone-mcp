# Development and setup details

The [README](../README.md#register) covers cloning and registering the server.
This guide covers development, registration scopes and options, and managing a
registration. Replace `/absolute/path/to/dangerzone-mcp` with your checkout.

## Development from a local checkout

From the repository root:

```sh
uv sync
uv run pre-commit install
uv run scripts/demo.py
```

The demo starts a real stdio server with a temporary catalog and exercises add,
call, edit, and remove. `uv run dangerzone-mcp` starts a normal server that
waits for MCP messages on stdin; an MCP client starts it this way for you.

Run the development checks with:

```sh
uv run pre-commit run --all-files
uv run pytest
uv build
```

CI runs the same steps with `uv sync --locked`, so commit lockfile changes.
The tests drive a real stdio server through the SDK client, including
restart-and-restore, a corrupt catalog under a running server, and two processes
writing the same file.

### Registration scopes and server options

```sh
claude mcp add --transport stdio --scope user dangerzone -- \
  uv run --project /absolute/path/to/dangerzone-mcp dangerzone-mcp
```

Choose `--scope user` for all your projects, `--scope local` for personal use in
the current project, or `--scope project` to share a `.mcp.json` configuration.
Run project-scoped commands from the consuming project's directory.
[Claude Code MCP scopes](https://code.claude.com/docs/en/mcp#mcp-installation-scopes)

`uv run --project` selects this package while keeping the client's working
directory, so catalog discovery works as described under
[Persistence](../README.md#persistence). Server options go after the final
`dangerzone-mcp`: `--project-dir /absolute/path/to/your/project` for a client
that starts servers elsewhere, or `--no-persist` to keep tools in memory. For
example:

```sh
claude mcp add --transport stdio --scope user dangerzone -- \
  uv run --project /absolute/path/to/dangerzone-mcp dangerzone-mcp --no-persist
```

The checkout is installed in editable mode. After editing server code or
changing launch arguments, reconnect the server through the client's `/mcp`
controls or start a fresh client session; a running process keeps the code it
loaded at startup. Changes made through `add_tool`, `edit_tool`, and
`remove_tool` take effect while it is running.

### Other MCP clients

Use the [README's JSON configuration](../README.md#other-mcp-clients) with the
absolute path to your checkout. GUI clients may need the absolute path to `uv`
as well; find it with `command -v uv`.

### Updating

Pull the checkout and reconnect:

```sh
git -C /absolute/path/to/dangerzone-mcp pull
```

If the launch fails after an update, run
`uv run --project /absolute/path/to/dangerzone-mcp dangerzone-mcp --help`
in a terminal first. This separates sync and build errors from MCP connection
errors.

## Verify or change a registration

Inspect the configured command and the active server:

```sh
claude mcp get dangerzone
```

Run `/mcp` in Claude Code and expect the three permanent tools plus any saved
custom tools. [Claude Code MCP commands](https://code.claude.com/docs/en/mcp#managing-your-servers)
document these checks.

To change the command, scope, or checkout path, remove the entry with the
scope you originally chose, then register again:

```sh
claude mcp remove --scope user dangerzone
```

Removing a client registration leaves `dangerzone.tools.json` intact.
