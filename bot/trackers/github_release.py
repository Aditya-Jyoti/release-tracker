"""GitHub releases tracker."""

from __future__ import annotations

import html
import logging
import re
import dataclasses
import fnmatch
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .base import (
    FetchError,
    InvalidTarget,
    RateLimited,
    TargetNotFound,
    Tracker,
    Version,
    excerpt,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"

_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_URL_FORM = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s?#]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_SSH_FORM = re.compile(r"^(?:ssh://)?git@github\.com[:/](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$", re.IGNORECASE)
_SHORT_FORM = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$")

# Special --tag values (case-insensitive). Anything else matches tag/release names.
TAG_LATEST = frozenset({"latest"})
TAG_RELEASE = frozenset({"release", "releases", "stable-release"})
TAG_PRE = frozenset({"pre", "prerelease", "pre-release"})


def _name_matches(pattern: str, v: Version) -> bool:
    names = (v.id.lower(), v.name.lower())
    if any(c in pattern for c in "*?["):
        return any(fnmatch.fnmatchcase(n, pattern) for n in names)
    return any(pattern in n for n in names)


def parse_repo(arg: str) -> str:
    """Parse ``owner/repo`` or a GitHub URL into lower-cased ``owner/repo``.

    GitHub names are case-insensitive, so lower-casing makes the same repo
    entered differently dedupe to one target.
    """
    s = arg.strip().strip("<>").strip()
    for pattern in (_URL_FORM, _SSH_FORM, _SHORT_FORM):
        m = pattern.match(s)
        if m:
            break
    else:
        raise InvalidTarget(f"Not a GitHub repository: {arg!r}. Use owner/repo or a github.com URL.")

    owner, repo = m["owner"], m["repo"]
    repo = repo.removesuffix(".git")
    if not _OWNER.match(owner) or not _REPO.match(repo) or repo in {".", ".."}:
        raise InvalidTarget(f"Not a valid GitHub repository name: {owner}/{repo}")
    return f"{owner}/{repo}".lower()


class GitHubReleaseTracker(Tracker):
    kind = "github"
    description = (
        "GitHub releases. Picks per repo: the release GitHub labels \"Latest\" → "
        "else the newest release → else the newest tag"
    )
    flags = {"--pre": "follow the newest release including pre-releases / RCs"}
    value_flags = {
        "--tag": "only notify for: latest (GitHub's Latest badge), release, pre-release, "
        "or any text/wildcard matched against tag names (e.g. stable, polkadot-stable*)",
    }

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token: str | None = None,
        fallback_to_tags: bool = True,
        api_base: str = API_BASE,
        per_page: int = 20,
        default_backoff: float = 60.0,
    ) -> None:
        self._client = client
        self._token = token
        self.fallback_to_tags = fallback_to_tags
        self._api = api_base.rstrip("/")
        self._per_page = per_page
        self._default_backoff = default_backoff
        # url -> (etag, decoded json). In-memory: after a restart the first
        # request per URL is unconditional, which is fine.
        self._etag_cache: dict[str, tuple[str, Any]] = {}
        self._blocked_until = 0.0

    # ----------------------------------------------------------------- interface

    def normalize(self, arg: str) -> str:
        return parse_repo(arg)

    def target_url(self, target: str) -> str:
        return f"{WEB_BASE}/{target}"

    def parse_options(self, flags: Sequence[str]) -> dict[str, Any]:
        options = super().parse_options(flags)
        if "tag" in options:
            options["tag"] = options["tag"].lower()
            if options.get("pre"):
                raise InvalidTarget("--pre can't be combined with --tag (use --tag pre-release for pre-releases only)")
        return options

    @staticmethod
    def _needs_latest_label(options: Mapping[str, Any]) -> bool:
        tag = options.get("tag")
        return tag in TAG_LATEST if tag else not options.get("pre")

    async def fetch(self, target: str, requested: Sequence[Mapping[str, Any]] = ()) -> list[Version]:
        releases = await self._get_json(f"/repos/{target}/releases", {"per_page": self._per_page})
        if not isinstance(releases, list):
            raise FetchError(f"unexpected releases payload for {target}")
        versions = [self._release_to_version(target, r) for r in releases if not r.get("draft")]
        versions = [v for v in versions if v is not None]
        if versions:
            # Only fetch the Latest label if some item tracking this repo uses it.
            if not requested or any(self._needs_latest_label(o) for o in requested):
                versions = await self._mark_latest(target, versions)
            return versions
        if not self.fallback_to_tags:
            return versions

        tags = await self._get_json(f"/repos/{target}/tags", {"per_page": 10})
        if not isinstance(tags, list):
            raise FetchError(f"unexpected tags payload for {target}")
        return [
            Version(
                id=t["name"],
                name=t["name"],
                url=f"{WEB_BASE}/{target}/releases/tag/{t['name']}",
                label="tag",
            )
            for t in tags
            if isinstance(t, dict) and t.get("name")
        ]

    def select_latest(self, versions: Sequence[Version], options: Mapping[str, Any]) -> Version | None:
        """Resolve what to follow for one item.

        * ``--tag latest|release|pre-release``: only that kind of release.
        * ``--tag <text or glob>``: newest release (or tag) whose name matches.
        * ``--pre``: the newest release, pre-releases included.
        * otherwise: the release GitHub labels "Latest" (maintainer-chosen, so
          backports/patches of older lines don't count) → else the newest
          non-pre-release → else (repo without releases) the newest tag.
        """
        tag = options.get("tag")
        if tag in TAG_LATEST:
            return next((v for v in versions if "latest" in v.markers), None)
        if tag in TAG_RELEASE:
            return next((v for v in versions if v.label == "release" and not v.prerelease), None)
        if tag in TAG_PRE:
            return next((v for v in versions if v.prerelease), None)
        if tag:
            return next((v for v in versions if _name_matches(tag, v)), None)
        if options.get("pre"):
            return versions[0] if versions else None
        latest = next((v for v in versions if "latest" in v.markers), None)
        return latest or next((v for v in versions if not v.prerelease), None)

    def format_message(self, target: str, version: Version) -> str:
        e = html.escape
        is_tag = version.label == "tag"
        head = "🏷 <b>New tag</b>" if is_tag else "🚀 <b>New release</b>"
        lines = [f'{head} in <a href="{e(self.target_url(target))}">{e(target)}</a>']

        title = f"<b>{e(version.name)}</b>"
        if version.name != version.id:
            title += f" (<code>{e(version.id)}</code>)"
        if version.prerelease:
            title += " · <i>pre-release</i>"
        lines.append(title)

        notes = excerpt(version.notes, 500)
        if notes:
            lines.append(f"<blockquote>{e(notes)}</blockquote>")
        if version.url:
            lines.append(f'<a href="{e(version.url)}">View {"tag" if is_tag else "release"}</a>')
        return "\n".join(lines)

    # ----------------------------------------------------------------- internals

    async def _mark_latest(self, target: str, versions: list[Version]) -> list[Version]:
        """Tag the release GitHub shows as "Latest" with the ``latest`` marker.

        GitHub's Latest is chosen by maintainers (``make_latest``), not simply the
        newest release, so it has to come from the dedicated endpoint.
        """
        try:
            data = await self._get_json(f"/repos/{target}/releases/latest")
        except TargetNotFound:
            return versions  # repo has only pre-releases/drafts: no Latest release
        latest = self._release_to_version(target, data)
        if latest is None:
            return versions
        marked = dataclasses.replace(latest, markers=latest.markers | {"latest"})
        if any(v.id == latest.id for v in versions):
            return [marked if v.id == latest.id else v for v in versions]
        return [*versions, marked]  # older than the page we fetched

    @staticmethod
    def _release_to_version(target: str, r: Any) -> Version | None:
        if not isinstance(r, dict) or not r.get("tag_name"):
            return None
        tag = r["tag_name"]
        return Version(
            id=tag,
            name=(r.get("name") or "").strip() or tag,
            url=r.get("html_url") or f"{WEB_BASE}/{target}/releases/tag/{tag}",
            notes=r.get("body") or "",
            published_at=r.get("published_at"),
            prerelease=bool(r.get("prerelease")),
        )

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        now = time.time()
        if now < self._blocked_until:
            raise RateLimited("GitHub rate limit backoff in effect", self._blocked_until)

        url = f"{self._api}{path}"
        cache_key = str(httpx.URL(url, params=params))
        cached = self._etag_cache.get(cache_key)
        headers = self._headers()
        if cached:
            headers["If-None-Match"] = cached[0]

        try:
            resp = await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchError(f"network error: {type(exc).__name__}: {exc}") from exc

        status = resp.status_code
        log.debug(
            "github response",
            extra={"path": path, "status": status, "ratelimit_remaining": resp.headers.get("x-ratelimit-remaining")},
        )

        if status == 304:
            if cached is None:
                raise FetchError("304 without a cached response")
            return cached[1]
        if status == 404:
            raise TargetNotFound(f"GitHub returned 404 for {path}")
        if status == 401:
            raise FetchError("GitHub returned 401 Unauthorized; check GITHUB_TOKEN")
        if status in (403, 429) and self._is_rate_limited(resp):
            retry_at = self._retry_at(resp)
            self._blocked_until = retry_at
            log.warning(
                "github rate limited, backing off",
                extra={"status": status, "retry_in_s": round(retry_at - time.time()), "path": path},
            )
            raise RateLimited(f"GitHub rate limited (HTTP {status})", retry_at)
        if status >= 400:
            raise FetchError(f"GitHub returned HTTP {status} for {path}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError(f"invalid JSON from GitHub for {path}") from exc

        etag = resp.headers.get("etag")
        if etag:
            self._etag_cache[cache_key] = (etag, data)
        return data

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers:
            return True
        return "rate limit" in resp.text.lower()

    def _retry_at(self, resp: httpx.Response) -> float:
        now = time.time()
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return now + int(retry_after)
        reset = resp.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit() and int(reset) > now:
            return float(int(reset)) + 1
        return now + self._default_backoff
