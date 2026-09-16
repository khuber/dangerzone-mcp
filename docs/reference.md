# Reference

Implementation and operational details. The [README](../README.md) covers
registration and trust; [setup.md](setup.md) covers development and advanced
setup.

## How it works

Four modules:

- [`server.py`](../src/dangerzone_mcp/server.py) is the MCP surface: the
  low-level SDK `Server`, the three built-ins, the `tools/list` handler, and
  the subscription bus that fans out change notifications.
- [`registry.py`](../src/dangerzone_mcp/registry.py) validates definitions and
  reads, modifies, and writes the catalog as a unit. Every invalid definition
  is reported as a `ValueError`.
- [`persistence.py`](../src/dangerzone_mcp/persistence.py) finds the project
  root and owns the file lock and atomic write.
- [`worker.py`](../src/dangerzone_mcp/worker.py) validates arguments and runs
  tool code in a fresh Python process per call. It does not import the server,
  so tool code cannot reach the registry; as ordinary Python it can still edit
  files, the catalog included.

## Management tools

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `add_tool` | `name`, `description`, `input_schema`, `source` | Register a new custom tool; reject duplicates. |
| `edit_tool` | `name`, `description`, `input_schema`, `source` | Replace all fields of an existing custom tool; reject unknown names. |
| `remove_tool` | `name` | Remove an existing custom tool. |

All three return `{"name": ..., "status": "added" | "edited" | "removed"}` as
structured content, advertise that shape as their `outputSchema`, and carry
`ToolAnnotations` marking them non-read-only and, for `edit_tool` and
`remove_tool`, destructive. Their names are reserved case-insensitively: a
request to add, edit, or remove `ADD_TOOL` fails the same way `add_tool` does.

## Custom tool definitions

For example, call `add_tool` with:

```json
{
  "name": "square",
  "description": "Square an integer",
  "input_schema": {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": false
  },
  "source": "def main(arguments):\n    return arguments['n'] ** 2"
}
```

The new `square` tool accepts `{"n": 7}` and returns structured content
`{"result": 49}` plus a matching text result. Call `edit_tool` with the same
four fields to replace the definition, or `remove_tool` with `{"name": "square"}`
to remove it. Failed validation leaves the catalog unchanged and sends no
notification. A call already in flight finishes with the definition it started
with.

`name` matches `^[a-zA-Z0-9_.-]{1,128}$`. `description` is 1 to 10,000
characters. `source` is 1 to 100,000 characters.

`source` must define exactly one top-level `main` function, synchronous or
asynchronous, taking one positional parameter and carrying no decorators.
Module-level code outside `main` runs each time the tool is called, before
`main`. The function receives the validated arguments dictionary and returns a
JSON-serializable value; `NaN`, infinity, and strings containing lone Unicode
surrogates are rejected. The source is parsed and compiled at registration and
at load, but executed only when the tool is called. A definition that exhausts
the parser's or validator's recursion or memory limits is rejected even if it
fits the character limits.

`input_schema` is JSON Schema 2020-12 with `"type": "object"` at the root. A
`$schema` keyword, if present, must name the 2020-12 dialect. `$ref` and
`$dynamicRef` must be local (`#...`), must resolve, and must not form cycles, so
recursive schemas are rejected. Reference targets are checked even when a JSON
pointer reaches a location normally used for data, such as `examples`. Nothing
is fetched over the network. Arguments are validated inside the worker before
any tool source executes, under the call's timeout; a failure is a tool result
with `isError: true` and the validator's message.

Custom tools are listed with an `outputSchema` describing the fixed envelope
`{"result": <any>}`.

## Catalog file

```json
{
  "version": 1,
  "tools": []
}
```

Each entry in `tools` has the same four fields `add_tool` accepts. Tools are
written sorted by name. `version` must be `1`; unknown keys are rejected.

On load, every entry is validated exactly as if it were being added, including
the protected-name check and the source compile. Duplicates fail the load. A
file that fails validation at startup stops the server with an error and is
never rewritten. A file that fails while the server is running leaves
`tools/list` returning only the built-ins and makes every management call fail
with the load error until the file is repaired; the file is not modified.

### Location

The launcher starts from `--project-dir`, which defaults to the current working
directory. It walks up from there to the nearest directory containing a `.git`
entry and uses that as the catalog directory. Linked worktrees and submodules
have their own `.git` file, so each gets its own catalog in its root. Outside a
repository, the starting directory is used. The directory must exist, and a
catalog that is a symbolic link is rejected at startup so a cloned repository
cannot point the server at a file outside the project. With `--no-persist` no
path is computed and no file or lock is touched.

### Writes and locking

Each add, edit, or remove takes the `dangerzone.tools.json.lock` file lock (5
second timeout), re-reads the catalog, applies the change, writes a temporary
file in the same directory, flushes and fsyncs it, and renames it over the
catalog. Reads for `tools/list` and `tools/call` take the same lock and re-read
the file, so a change made by another process is visible on the next request
without a notification. A failed write leaves the previous catalog in place,
reports the error as a tool result, and sends no notification.

Replacing an existing catalog preserves its permission bits. A newly created
catalog is readable and writable only by its owner (`0600`).

### Git ignore rules

The lock sidecar stays next to the catalog. In a consuming project, ignore it
along with the temporary files:

```gitignore
dangerzone.tools.json.lock
.dangerzone.tools.json.*.tmp
```

Also ignore `dangerzone.tools.json` for personal experiments, or deliberately
commit reviewed tools to share them. This repository ignores its own catalog.

## Execution

The standard library and packages installed in the server's environment are
available to tool code.

Each call runs `python -I worker.py` as a fresh subprocess using the server's
interpreter. The worker reads `{"source", "input_schema", "arguments"}` as JSON
on stdin and writes `{"result": ...}` or `{"error": ...}` as JSON on stdout. Inside the
worker `sys.stdout` is redirected to stderr, so `print` output reaches the
server's log at WARNING level; writes to file descriptor 1 or `sys.__stdout__`
bypass the redirect and corrupt the result channel, which the server reports as
an explicit tool error. Uncaught exceptions, including `SystemExit`, come back
as errors with the traceback. A non-zero exit status is reported with whatever
the worker wrote to stderr.

The default timeout is 30 seconds (`--timeout SECONDS`, which must be finite
and positive), including worker startup, argument validation, and tool code.
On timeout or client cancellation the worker is killed and reaped. Completion
depends on the worker exiting, so background children inheriting stdout or
stderr do not delay the result. On POSIX, remaining processes in the worker's
process group are killed after every call, including successful calls.
Deliberately detached processes are outside that group. There is no limit on
concurrent workers.

## Notifications

Successful mutations publish `ToolsListChanged` through the SDK's
`InMemorySubscriptionBus` to every client subscribed via `subscriptions/listen`.
For sessions negotiated on a protocol version older than the subscription
mechanism, the server also sends the legacy `notifications/tools/list_changed`
to the calling session. `tools/list` responses set `ttlMs: 0` and
`cacheScope: private` and are sorted by name. Only the process that made the
change sends notifications. Separate client sessions run their own server
processes but can share the same catalog file. Other sessions discover added or
removed tools on their next tool-list request; calls to existing tools read the
latest saved definition. There is no file watcher to trigger a refresh in those
other sessions.

## Example scope

Kept out of scope for this example:

- File watching to notify clients of changes made by another server process.
- Catalog caching: every list and call re-reads and re-validates all definitions,
  so that work grows linearly with the number of tools.
- A cap on concurrent workers.
- Other tool languages or reusable workers: tools are Python only, and each
  call starts a fresh interpreter.

## Protocol errors versus tool errors

Unknown tool names and any pagination cursor are JSON-RPC `-32602` invalid
params errors; the server returns the whole catalog in one page. Argument
validation failures, worker failures, timeouts, catalog load failures during a
management call, and protected-name violations are tool results with
`isError: true`.

## In-process use

`create_server(timeout=30.0, storage_path=None)` returns an SDK `Server`.
Passing a `Path` enables persistence at that file; omitting it keeps an
isolated in-memory catalog, which the tests use. Its default initialization
options advertise `tools.listChanged` for both legacy and modern clients.
`serve_stdio(server)` runs it over stdio with those options.
