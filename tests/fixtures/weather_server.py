"""Weather fixture MCP server. Has a `search` tool to collide with other servers."""

from __future__ import annotations

from mcp.server import MCPServer

mcp = MCPServer("Weather")


@mcp.tool()
def forecast(city: str, days: int = 3) -> str:
    """Get the weather forecast for a city."""
    return f"Sunny in {city} for {days} days."


@mcp.tool()
def search(query: str) -> str:
    """Search weather stations by name."""
    return f"2 stations match {query!r}."


if __name__ == "__main__":
    mcp.run()
