"""Bookshop fixture MCP server: tools, resources, templates, prompts.

Run over stdio:  python tests/fixtures/bookshop_server.py
Run over HTTP:   python tests/fixtures/bookshop_server.py --http PORT
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

mcp = MCPServer("Bookshop", instructions="Search the catalog before recommending a book. Second sentence.")

GENRES = ["fiction", "non-fiction", "poetry"]


class Book(BaseModel):
    title: str
    author: str
    year: int


@mcp.tool(title="Search the catalog")
def search_books(query: str, limit: int = 10) -> str:
    """Search the catalog by title or author.

    Returns a short summary of matching books.
    """
    return f"Found 3 books matching {query!r} (showing up to {limit})."


@mcp.tool()
def lookup_book(title: str) -> Book:
    """Look up a book by its exact title."""
    if title != "Dune":
        raise ToolError(f"No book titled {title!r} in the catalog.")
    return Book(title="Dune", author="Frank Herbert", year=1965)


@mcp.tool()
def add_review(
    book_title: str, rating: int, comment: str | None = None, tags: list[str] | None = None
) -> str:
    """Add a customer review to a book."""
    return f"Review saved for {book_title} ({rating}/5)."


@mcp.tool()
def cover_image() -> list:
    """Return a tiny PNG cover image."""
    from mcp.types import ImageContent

    # 1x1 transparent PNG
    data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    return [ImageContent(type="image", data=data, mime_type="image/png")]


@mcp.tool()
def crash() -> str:
    """Exit the server process abruptly (for crash tests)."""
    import os

    os._exit(3)


@mcp.resource("catalog://genres")
def genres() -> list[str]:
    """The genres the catalog is organised by."""
    return GENRES


@mcp.resource("catalog://genres/{genre}")
def books_in_genre(genre: str) -> str:
    """Every title we stock in one genre."""
    return f"3 books filed under {genre}."


@mcp.prompt(title="Recommend a book")
def recommend(genre: str) -> str:
    """Ask for a recommendation in a genre."""
    return f"Recommend one {genre} book from the catalog and say why."


if __name__ == "__main__":
    print("bookshop starting", file=sys.stderr)  # must never reach mcpc stdout
    if len(sys.argv) >= 3 and sys.argv[1] == "--http":
        import uvicorn

        uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    else:
        mcp.run()
