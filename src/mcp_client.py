import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters


def server_commands() -> dict[str, list[str]]:
    """The commands that start the two MCP servers."""
    ddg = shutil.which("duckduckgo-mcp-server") or str(Path(sys.executable).with_name("duckduckgo-mcp-server.exe"))
    playwright = shutil.which("playwright-mcp")
    # Browser artifacts go to the system temp dir, so the workspace stays clean.
    out_dir = ["--output-dir", str(Path(tempfile.gettempdir()) / "slm-playwright-mcp")]
    return {
        "duckduckgo-mcp-server": [ddg],
        "playwright-mcp": (
            [playwright, "--headless", *out_dir] if playwright
            else ["npx", "-y", "@playwright/mcp@latest", "--headless", *out_dir]
        ),
    }


async def show_tools(name: str, command: list[str]) -> None:
    """Connect to one MCP server and print the tools it exposes."""
    params = StdioServerParameters(command=command[0], args=command[1:])
    try:
        async with Client(params) as client:
            result = await client.list_tools()
    except Exception as exc:
        print(f"[mcp] {name} FAILED to start: {type(exc).__name__}: {exc}")
        return
    print(f"[mcp] {name} started, tools exposed:")
    for tool in result.tools:
        print(f"  - {tool.name}")


async def main() -> None:
    """Start each MCP server in turn and show its tools."""
    for name, command in server_commands().items():
        await show_tools(name, command)


if __name__ == "__main__":
    asyncio.run(main())
