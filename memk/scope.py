"""Scope: who a memory belongs to, and who may read it.

A scope id is a pipe-joined tuple. The user segment is optional, because some
knowledge belongs to the agent rather than to a person -- and getting that
distinction wrong in the restrictive direction makes globally learned facts
unrecallable, which is the bug we hit before.

Readable scopes are always a superset of writable scope: reads may consult the
agent level, writes never silently land there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

SEGMENTS = ("platform", "workspace", "agent", "user")


@dataclass(frozen=True)
class Scope:
    platform: str = "local"
    workspace: str = "default"
    agent: str = "default"
    user: str = ""

    def id(self) -> str:
        parts = [
            f"platform:{_norm(self.platform)}",
            f"workspace:{_norm(self.workspace)}",
            f"agent:{_norm(self.agent)}",
        ]
        if self.user:
            parts.append(f"user:{_norm(self.user)}")
        return "|".join(parts)

    def agent_level_id(self) -> str:
        """The scope for knowledge that is not about one person."""

        return "|".join(
            [
                f"platform:{_norm(self.platform)}",
                f"workspace:{_norm(self.workspace)}",
                f"agent:{_norm(self.agent)}",
            ]
        )

    def readable(self) -> list[str]:
        """Every scope this reader may consult, most specific first."""

        out = [self.id()]
        agent_level = self.agent_level_id()
        if agent_level not in out:
            out.append(agent_level)
        return out


def _norm(value: str) -> str:
    text = str(value or "").strip().lower()
    return text or "default"


def writable_scope(scope: Scope, target: str) -> str:
    """Resolve the write scope for a target.

    Durable targets are per-user when a user is known; ``general`` is scratch and
    stays in the caller's own scope. A durable target with no user segment writes
    at agent level, which is the only honest place for it.
    """

    if target == "general":
        return scope.id()
    if scope.user:
        return scope.id()
    return scope.agent_level_id()


def parse(scope_id: str) -> Scope:
    fields: dict[str, str] = {}
    for chunk in str(scope_id or "").split("|"):
        if ":" in chunk:
            key, _, value = chunk.partition(":")
            fields[key.strip()] = value.strip()
    return Scope(
        platform=fields.get("platform", "default"),
        workspace=fields.get("workspace", "default"),
        agent=fields.get("agent", "default"),
        user=fields.get("user", ""),
    )


def placeholders(values: Iterable[str]) -> tuple[str, list[str]]:
    items = list(values)
    return ",".join("?" * len(items)), items


def explain(scope: Scope) -> dict[str, object]:
    return {
        "scope_id": scope.id(),
        "agent_level": scope.agent_level_id(),
        "readable": scope.readable(),
        "writes": {
            target: writable_scope(scope, target)
            for target in ("user", "memory", "project", "ops", "general")
        },
    }
