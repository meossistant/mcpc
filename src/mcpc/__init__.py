"""mcpc: command-line MCP client."""

try:
    from mcpc._version import __version__
except ImportError:  # pragma: no cover - source checkout without build
    __version__ = "0.1.0.dev0"

__all__ = ["__version__"]
