"""Shared primitives for the business tool layer.

Kept in its own module so handlers can raise and build these without importing
the adapter (which imports the handler registry).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone


class ToolConflict(RuntimeError):
    """The request key, resource version or prepared snapshot no longer matches."""


class ToolRefused(ValueError):
    """A business precondition refused the call before anything was written.

    The adapter reports these as ``failed`` with the given stable ``code``; a
    caller can therefore distinguish "the business said no" from "we do not
    know whether the effect happened".
    """

    def __init__(self, code: str, message: str, issues: list | None = None) -> None:
        self.code = code
        self.issues = list(issues or [])
        super().__init__(message)


@dataclass(frozen=True)
class ResourceSnapshot:
    """What the workspace policy service reports about one business resource."""

    id: str
    project_id: str
    owner_id: int
    version: str


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
