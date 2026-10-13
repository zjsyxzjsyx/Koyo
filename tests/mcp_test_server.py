"""Tiny stdio MCP server used by the client tests; no network."""
import asyncio

from mcp.server.fastmcp import FastMCP

server = FastMCP("test")


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"回声：{text}"


@server.tool()
def fail(text: str) -> str:
    """Always fail."""
    raise ValueError(f"故意失败：{text}")


@server.tool()
async def slow(seconds: float) -> str:
    """Sleep, then answer."""
    await asyncio.sleep(seconds)
    return "慢回答"


@server.tool()
def delete_notes(path: str) -> str:
    """A tool the whitelist never allows."""
    return f"would delete {path}"


if __name__ == "__main__":
    server.run()
