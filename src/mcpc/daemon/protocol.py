"""Wire protocol between the CLI and the daemon.

Newline-delimited UTF-8 JSON over loopback TCP. Exactly one request and one response per connection.
A request carries the shared secret token; the daemon closes the connection without answering when
the token is wrong. A request may carry a `ResolvedServer`, which contains resolved secrets: it is
only ever held in memory and sent over loopback.
"""

from __future__ import annotations

from typing import Any, Literal

from anyio.abc import ByteReceiveStream, ByteSendStream
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel, ConfigDict, Field

from mcpc.catalog.models import ServerSnapshot
from mcpc.errors import McpcError
from mcpc.executor import ResolvedServer

__all__ = [
    "MAX_MESSAGE_BYTES",
    "VERSION_MISMATCH_MESSAGE",
    "Header",
    "Method",
    "Request",
    "Response",
    "StateFile",
    "read_line",
    "write_line",
]

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
VERSION_MISMATCH_MESSAGE = "daemon version mismatch"

Method = Literal["ping", "status", "stop", "snapshot", "call_tool", "read_resource", "get_prompt"]


class StateFile(BaseModel):
    """Contents of `paths.daemon_state_file()`. Holds the auth token, so it must stay user-private."""

    pid: int
    port: int
    token: str
    version: str
    started_at: str


class Header(BaseModel):
    """The part of a request every daemon version can parse; used for auth and version checks."""

    model_config = ConfigDict(extra="ignore")
    token: str
    version: str
    method: str


class Request(BaseModel):
    """One CLI -> daemon message. `params` depends on `method`:

    call_tool {tool, arguments}, read_resource {uri}, get_prompt {name, arguments}; others take none.
    """

    token: str
    version: str
    method: Method
    server: ResolvedServer | None = None
    prior: ServerSnapshot | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    """One daemon -> CLI message: `result` when `ok`, else `error` (an `McpcError.to_dict()`)."""

    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None

    @classmethod
    def success(cls, result: Any) -> Response:
        return cls(ok=True, result=result)

    @classmethod
    def failure(cls, err: McpcError) -> Response:
        return cls(ok=False, error=err.to_dict())


async def read_line(stream: ByteReceiveStream, max_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
    """Read one newline-terminated message (without the newline).

    Raises anyio.EndOfStream if the peer closes first and anyio.DelimiterNotFound if the message
    exceeds `max_bytes`.
    """
    buffered = stream if isinstance(stream, BufferedByteReceiveStream) else BufferedByteReceiveStream(stream)
    return await buffered.receive_until(b"\n", max_bytes)


async def write_line(stream: ByteSendStream, message: BaseModel) -> None:
    """Serialize `message` as a single JSON line and send it."""
    data = message.model_dump_json().encode("utf-8")
    await stream.send(data + b"\n")
