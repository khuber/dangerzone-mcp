"""Demonstrate dynamic discovery and protected management over real stdio."""

import asyncio
import sys
from tempfile import TemporaryDirectory

from mcp import Client, StdioServerParameters


async def main(project_dir: str) -> None:
    """Add, call, edit, and remove a tool while observing catalog notifications."""
    target = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dangerzone_mcp", "--project-dir", project_dir],
    )
    definition = {
        "name": "greet",
        "description": "Greet a person",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "source": 'def main(arguments):\n    return "Hello, " + arguments["name"]',
    }
    async with Client(target) as client:
        print("Protocol:", client.protocol_version)
        print("Initial tools:", [t.name for t in (await client.list_tools()).tools])
        async with client.listen(tools_list_changed=True) as subscription:
            for operation in ("add_tool", "edit_tool", "remove_tool"):
                if operation == "edit_tool":
                    definition["source"] = (
                        'def main(arguments):\n    return "Welcome, " + arguments["name"]'
                    )
                args = {"name": "greet"} if operation == "remove_tool" else definition
                result = await client.call_tool(operation, args)
                if result.is_error:
                    raise RuntimeError(result.content)
                async with asyncio.timeout(5):
                    event = await anext(aiter(subscription))
                print(operation, result.structured_content, type(event).__name__)
                print("Tools:", [t.name for t in (await client.list_tools()).tools])
                if operation != "remove_tool":
                    called = await client.call_tool("greet", {"name": "Ada"})
                    print("greet:", called.structured_content)
        protected = await client.call_tool("remove_tool", {"name": "edit_tool"})
        print("Built-in removal rejected:", protected.is_error)


if __name__ == "__main__":
    with TemporaryDirectory() as project_dir:
        asyncio.run(main(project_dir))
