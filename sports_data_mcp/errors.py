"""The one error shape every tool returns.

A failing tool call comes back as an MCP tool error (``is_error=True``) whose
text content is this JSON document::

    {"error": {"kind": "<kind>", "message": "<human readable>", "retry_after_s": <int|null>}}

``kind`` is one of :data:`KINDS`:

``not_found``
    The API answered 404: no such player, game, team or endpoint.
``rate_limited``
    The API answered 429 twice. ``retry_after_s`` carries the ``Retry-After``
    header (in whole seconds) when the API sent one, else null.
``upstream``
    The API answered 5xx (or an unexpected 4xx) twice, returned a body that was
    not JSON, or could not be reached at all (timeout, DNS, connection reset).
``bad_input``
    The arguments cannot be sent: short names, bad dates, ambiguous teams,
    non-positive ids, or an HTTP 400 from the API.
``offline``
    The server is in offline mode and the response is not in the cache.

:class:`SportsDataError` is the :class:`~mcp.server.mcpserver.exceptions.ToolError`
subclass that carries the shape; :func:`from_api_error` and :func:`bad_input`
build it, and :func:`parse_tool_error` reads it back out of a tool result.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from sports_data_mcp.mlb.client import APIError

Kind = Literal["not_found", "rate_limited", "upstream", "bad_input", "offline"]
KINDS: tuple[str, ...] = ("not_found", "rate_limited", "upstream", "bad_input", "offline")


class SportsDataError(ToolError):
    """A tool failure in the documented shape.

    ``str(exc)`` is the JSON document, so the MCP SDK puts it in the error
    result's text content verbatim (after its ``Error executing tool <name>:``
    prefix). ``payload`` is the same document as a dict.
    """

    def __init__(self, kind: str, message: str, retry_after_s: int | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown error kind {kind!r}; expected one of {', '.join(KINDS)}")
        self.kind: str = kind
        self.message: str = message
        self.retry_after_s: int | None = None if retry_after_s is None else int(retry_after_s)
        super().__init__(json.dumps(self.payload, separators=(", ", ": ")))

    @property
    def payload(self) -> dict[str, Any]:
        return {"error": {"kind": self.kind, "message": self.message, "retry_after_s": self.retry_after_s}}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SportsDataError(kind={self.kind!r}, message={self.message!r}, retry_after_s={self.retry_after_s!r})"


def bad_input(message: str) -> SportsDataError:
    """A ``bad_input`` error for arguments that cannot be sent to the API."""
    return SportsDataError("bad_input", message)


def from_api_error(exc: APIError) -> SportsDataError:
    """Translate the client's :class:`APIError` (already classified) into the tool shape."""
    return SportsDataError(exc.kind, exc.message, exc.retry_after_s)


def parse_tool_error(text: str) -> dict[str, Any]:
    """Return the ``{"error": {...}}`` document embedded in a tool error's text.

    The MCP SDK prefixes the message with ``Error executing tool <name>: ``;
    this finds the JSON object regardless of prefix.

    Raises:
        ValueError: if ``text`` carries no such document.
    """
    start = text.find('{"error"')
    if start < 0:
        raise ValueError(f"no error document in {text!r}")
    try:
        doc, _end = json.JSONDecoder().raw_decode(text, start)
    except ValueError as exc:
        raise ValueError(f"malformed error document in {text!r}") from exc
    error = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(error, dict) or set(error) != {"kind", "message", "retry_after_s"}:
        raise ValueError(f"malformed error document in {text!r}")
    return doc
