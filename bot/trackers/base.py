"""Tracker abstraction.

A tracker knows how to:
  * turn user input into a canonical ``target`` string   (``normalize``)
  * parse per-item command flags into an options dict     (``parse_options``)
  * fetch the current candidate versions for a target     (``fetch``)
    (it also receives the options of every item tracking that target, so it
    can fetch extra data only when some item needs it)
  * pick the version that matters for one tracked item    (``select_latest``)
  * render a notification message                         (``format_message``)

``fetch`` is called once per unique target per cycle; ``select_latest`` is then
applied per tracked item, so items with different options (e.g. ``--pre``)
share a single network request.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar


# --------------------------------------------------------------------------- errors


class TrackerError(Exception):
    """Base class for all tracker errors."""


class InvalidTarget(TrackerError):
    """The user-supplied target or flags could not be parsed. Message is user-facing."""


class TargetNotFound(TrackerError):
    """The target does not exist upstream (e.g. HTTP 404)."""


class RateLimited(TrackerError):
    """Upstream is rate-limiting us. ``retry_at`` is a unix timestamp."""

    def __init__(self, message: str, retry_at: float) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class FetchError(TrackerError):
    """Transient failure (network error, 5xx, bad payload). Retry next cycle."""


# --------------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class Version:
    """One observed version of a target.

    ``id`` is the stable identifier used for change detection and stored in the
    DB (a tag name, a digest, a feed entry GUID, a content hash, ...).
    """

    id: str
    name: str
    url: str | None = None
    notes: str = ""
    published_at: str | None = None
    prerelease: bool = False
    label: str = "release"  # what kind of thing this is, for display ("release", "tag", ...)
    #: Tracker-specific markers, e.g. ``{"latest"}`` for GitHub's "Latest" release.
    markers: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- interface


class Tracker(ABC):
    #: Short identifier stored in the DB ``kind`` column and used in commands.
    kind: ClassVar[str]
    #: Human-readable description for /help.
    description: ClassVar[str] = ""
    #: Boolean flags accepted by ``parse_options`` mapped to their help text.
    flags: ClassVar[Mapping[str, str]] = {}
    #: Flags taking a value (``--name value`` / ``--name=value``) mapped to help text.
    value_flags: ClassVar[Mapping[str, str]] = {}

    @abstractmethod
    def normalize(self, arg: str) -> str:
        """Return the canonical target for user input, or raise ``InvalidTarget``."""

    def parse_options(self, flags: Sequence[str]) -> dict[str, Any]:
        """Turn ``--flag`` / ``--flag=value`` arguments into an options dict.

        Boolean flags become ``True``; value flags keep their (stripped) value.
        """
        options: dict[str, Any] = {}
        for flag in flags:
            name, has_value, value = flag.partition("=")
            key = name.removeprefix("--").replace("-", "_")
            if name in self.value_flags:
                value = value.strip()
                if not value:
                    raise InvalidTarget(f"{name} needs a value, e.g. {name} something")
                options[key] = value
            elif name in self.flags and not has_value:
                options[key] = True
            else:
                allowed = ", ".join([*self.flags, *self.value_flags]) or "none"
                raise InvalidTarget(f"Unknown option {name!r} (allowed: {allowed})")
        return options

    @abstractmethod
    async def fetch(self, target: str, requested: Sequence[Mapping[str, Any]] = ()) -> list[Version]:
        """Return candidate versions, newest first. May raise ``TrackerError`` subclasses.

        ``requested`` holds the options of every item tracking ``target``. Trackers
        may use it to fetch extra data only when some item needs it; most ignore it.
        """

    def select_latest(self, versions: Sequence[Version], options: Mapping[str, Any]) -> Version | None:
        """Pick the version relevant to an item with ``options``. Default: newest."""
        return versions[0] if versions else None

    async def fetch_latest(self, target: str, options: Mapping[str, Any] | None = None) -> Version | None:
        options = options or {}
        return self.select_latest(await self.fetch(target, [options]), options)

    @abstractmethod
    def format_message(self, target: str, version: Version) -> str:
        """Telegram HTML message announcing ``version``."""

    def target_url(self, target: str) -> str | None:
        """Link to the target, for /list. Optional."""
        return None


# --------------------------------------------------------------------------- helpers

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1")
_BLANK_LINES = re.compile(r"\n{3,}")


def excerpt(text: str | None, limit: int = 500) -> str:
    """Lightly de-markdown ``text`` and truncate to ``limit`` chars on a word boundary.

    Returns plain text; the caller is responsible for HTML-escaping it.
    """
    if not text:
        return ""
    s = text.replace("\r\n", "\n")
    s = _HTML_COMMENT.sub("", s)
    s = _MD_IMAGE.sub("", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _HTML_TAG.sub("", s)
    s = _HEADING.sub("", s)
    s = _EMPHASIS.sub(r"\2", s)
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = _BLANK_LINES.sub("\n\n", s).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" \n.,;:") + "…"
