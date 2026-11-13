"""Change detection: fetch each unique target once, compare per item, notify."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .db import Database, TrackedItem
from .trackers import RateLimited, TargetNotFound, TrackerError, TrackerRegistry, Version

log = logging.getLogger(__name__)

#: ``send(chat_id, html_text) -> consumed``. Return True when the message was
#: delivered *or* failed permanently (e.g. bot blocked) so the version is marked
#: as seen; return False on transient failures to retry next cycle.
SendFunc = Callable[[int, str], Awaitable[bool]]


@dataclass
class CheckReport:
    targets: int = 0
    items: int = 0
    notifications: int = 0
    errors: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def rate_limited(self) -> bool:
        return any("rate limit" in e.lower() for e in self.errors.values())


def detect_change(item: TrackedItem, latest: Version | None) -> bool:
    """True if ``latest`` is a version this item hasn't been notified about.

    ``latest is None`` (nothing published / everything filtered out) is never a
    change. A missing baseline with a version present *is* a change: the repo
    had no releases when tracked and has since published one.
    """
    return latest is not None and latest.id != item.last_version


class Checker:
    def __init__(self, db: Database, registry: TrackerRegistry, concurrency: int = 4) -> None:
        self.db = db
        self.registry = registry
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, send: SendFunc) -> CheckReport:
        async with self._lock:
            return await self._run(send)

    async def _fetch(self, kind: str, target: str, items: list[TrackedItem]) -> list[Version]:
        tracker = self.registry.get(kind)
        async with self._sem:
            return await tracker.fetch(target, [i.options for i in items])

    async def _run(self, send: SendFunc) -> CheckReport:
        groups: dict[tuple[str, str], list[TrackedItem]] = defaultdict(list)
        for item in self.db.list_items():
            groups[(item.kind, item.target)].append(item)

        report = CheckReport(targets=len(groups), items=sum(map(len, groups.values())))
        if not groups:
            return report

        keys = list(groups)
        results = await asyncio.gather(*(self._fetch(k, t, groups[(k, t)]) for k, t in keys), return_exceptions=True)

        for (kind, target), result in zip(keys, results, strict=True):
            items = groups[(kind, target)]
            ctx = {"kind": kind, "target": target}

            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                report.errors[(kind, target)] = str(result) or type(result).__name__
                if isinstance(result, RateLimited):
                    log.info("skipped: rate limited", extra=ctx)
                elif isinstance(result, TargetNotFound):
                    log.warning("target not found upstream", extra=ctx)
                elif isinstance(result, KeyError):
                    log.error("no tracker registered for kind", extra=ctx)
                else:
                    log.warning("fetch failed", extra={**ctx, "error": str(result)}, exc_info=not isinstance(result, TrackerError))
                continue

            tracker = self.registry.get(kind)
            unchanged: list[int] = []
            for item in items:
                latest = tracker.select_latest(result, item.options)
                if not detect_change(item, latest):
                    unchanged.append(item.id)
                    continue
                assert latest is not None
                log.info(
                    "new version detected",
                    extra={**ctx, "chat_id": item.chat_id, "old": item.last_version, "new": latest.id},
                )
                try:
                    consumed = await send(item.chat_id, tracker.format_message(target, latest))
                except Exception:
                    log.exception("send raised", extra={**ctx, "chat_id": item.chat_id})
                    consumed = False
                if consumed:
                    self.db.set_version(item.id, latest.id)
                    report.notifications += 1
            self.db.mark_checked(unchanged)

        log.info(
            "check cycle finished",
            extra={
                "targets": report.targets,
                "items": report.items,
                "notifications": report.notifications,
                "errors": len(report.errors),
            },
        )
        return report

