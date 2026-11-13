"""Tracker registry and command-argument parsing."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .base import (
    FetchError,
    InvalidTarget,
    RateLimited,
    TargetNotFound,
    Tracker,
    TrackerError,
    Version,
)

__all__ = [
    "FetchError",
    "InvalidTarget",
    "ParsedArgs",
    "RateLimited",
    "TargetNotFound",
    "Tracker",
    "TrackerError",
    "TrackerRegistry",
    "Version",
]


@dataclass(frozen=True)
class ParsedArgs:
    tracker: Tracker
    target: str  # raw, not yet normalized
    flags: tuple[str, ...]


class TrackerRegistry:
    """Maps ``kind`` -> tracker instance."""

    def __init__(self, trackers: Iterable[Tracker], default_kind: str | None = None) -> None:
        self._trackers: dict[str, Tracker] = {}
        for t in trackers:
            if t.kind in self._trackers:
                raise ValueError(f"duplicate tracker kind {t.kind!r}")
            self._trackers[t.kind] = t
        if not self._trackers:
            raise ValueError("at least one tracker is required")
        self.default_kind = default_kind or next(iter(self._trackers))
        if self.default_kind not in self._trackers:
            raise ValueError(f"unknown default kind {self.default_kind!r}")

    def get(self, kind: str) -> Tracker:
        return self._trackers[kind]

    def __contains__(self, kind: object) -> bool:
        return kind in self._trackers

    @property
    def trackers(self) -> list[Tracker]:
        return list(self._trackers.values())

    def parse_args(self, args: Sequence[str]) -> ParsedArgs:
        """Parse ``[kind] <target> [--flag ...]``.

        The kind is optional and defaults to ``default_kind``. Raises
        ``InvalidTarget`` with a user-facing message on bad input.
        """
        value_flags = {f for t in self._trackers.values() for f in t.value_flags}
        flags: list[str] = []
        positional: list[str] = []
        i = 0
        while i < len(args):
            arg = _fix_dashes(args[i])
            if arg.startswith("--"):
                name, eq, value = arg.partition("=")
                name = name.lower()
                nxt = _fix_dashes(args[i + 1]) if i + 1 < len(args) else None
                if not eq and name in value_flags and nxt is not None and not nxt.startswith("--"):
                    eq, value = "=", nxt
                    i += 1
                flags.append(f"{name}{eq}{value}")
            else:
                positional.append(arg)
            i += 1

        if len(positional) == 1:
            kind, target = self.default_kind, positional[0]
        elif len(positional) == 2 and positional[0].lower() in self._trackers:
            kind, target = positional[0].lower(), positional[1]
        elif not positional:
            raise InvalidTarget("Missing target.")
        else:
            kinds = ", ".join(self._trackers)
            raise InvalidTarget(f"Expected [kind] <target> [--options]; known kinds: {kinds}")
        return ParsedArgs(self._trackers[kind], target, tuple(flags))


def _fix_dashes(arg: str) -> str:
    """Telegram clients often autocorrect ``--`` into an em/en dash."""
    for dash in ("—", "–"):
        if arg.startswith(dash):
            return "--" + arg[len(dash):]
    return arg
