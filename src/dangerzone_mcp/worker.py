"""Run tool code in a fresh process, away from the MCP server."""

import asyncio
import json
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry


def main() -> None:
    """Read one execution request and write one JSON result."""
    payload = json.load(sys.stdin)
    namespace: dict[str, Any] = {"__name__": "__dynamic_tool__"}
    try:
        try:
            # An empty registry keeps every reference local; nothing is fetched.
            validator = Draft202012Validator(payload["input_schema"], registry=Registry())
            validator.validate(payload["arguments"])
        except ValidationError as exc:
            sys.stdout.write(json.dumps({"error": exc.message}))
            return
        with redirect_stdout(sys.stderr):
            exec(compile(payload["source"], "<dynamic-tool>", "exec"), namespace)
            result = namespace["main"](payload["arguments"])
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
        response = json.dumps({"result": result}, allow_nan=False)
    except BaseException as exc:
        details = traceback.format_exc()
        sys.stderr.write(details)
        response = json.dumps({"error": f"{type(exc).__name__}: {exc}\n{details}"})
    sys.stdout.write(response)


if __name__ == "__main__":
    main()
