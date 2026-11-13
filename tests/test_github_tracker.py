import time

import httpx
import pytest

from bot.trackers import FetchError, RateLimited, TargetNotFound, Version
from bot.trackers.base import excerpt
from bot.trackers.github_release import GitHubReleaseTracker


def release(tag, *, name=None, pre=False, draft=False, body="notes"):
    return {
        "tag_name": tag,
        "name": name if name is not None else f"Release {tag}",
        "prerelease": pre,
        "draft": draft,
        "body": body,
        "html_url": f"https://github.com/o/r/releases/tag/{tag}",
        "published_at": "2026-01-01T00:00:00Z",
    }


class Router:
    """Tiny programmable GitHub API stub that records requests."""

    def __init__(self):
        self.routes: dict[str, list[httpx.Response] | httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    def add(self, path, *responses):
        self.routes[path] = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get(request.url.path)
        if not queue:
            return httpx.Response(404, json={"message": "Not Found"})
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, Exception):
            raise resp
        return resp

    def calls(self, path):
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture
def router():
    return Router()


@pytest.fixture
async def make(router):
    clients = []

    def _make(**kw):
        client = httpx.AsyncClient(transport=httpx.MockTransport(router))
        clients.append(client)
        return GitHubReleaseTracker(client, **kw)

    yield _make
    for c in clients:
        await c.aclose()


REL = "/repos/o/r/releases"
LATEST = "/repos/o/r/releases/latest"
TAGS = "/repos/o/r/tags"


async def test_fetch_skips_drafts_and_parses(router, make):
    router.add(REL, httpx.Response(200, json=[release("v3", draft=True), release("v2", pre=True), release("v1")]))
    versions = await make().fetch("o/r")
    assert [v.id for v in versions] == ["v2", "v1"]
    assert versions[0].prerelease and not versions[1].prerelease
    assert versions[1].name == "Release v1" and versions[1].url.endswith("/v1")


async def test_select_latest_respects_pre_option(router, make):
    router.add(REL, httpx.Response(200, json=[release("v2-rc1", pre=True), release("v1")]))
    t = make()
    versions = await t.fetch("o/r")
    assert t.select_latest(versions, {}).id == "v1"
    assert t.select_latest(versions, {"pre": True}).id == "v2-rc1"
    assert t.select_latest([Version("x", "x", prerelease=True)], {}) is None
    assert (await t.fetch_latest("o/r", {"pre": True})).id == "v2-rc1"


async def test_empty_name_falls_back_to_tag(router, make):
    router.add(REL, httpx.Response(200, json=[release("v1", name="")]))
    assert (await make().fetch("o/r"))[0].name == "v1"


async def test_no_releases_falls_back_to_tags(router, make):
    router.add(REL, httpx.Response(200, json=[]))
    router.add(TAGS, httpx.Response(200, json=[{"name": "1.2.0"}, {"name": "1.1.0"}]))
    versions = await make().fetch("o/r")
    assert [v.id for v in versions] == ["1.2.0", "1.1.0"]
    assert versions[0].label == "tag"


async def test_no_releases_without_fallback(router, make):
    router.add(REL, httpx.Response(200, json=[]))
    assert await make(fallback_to_tags=False).fetch("o/r") == []
    assert not router.calls(TAGS)


async def test_only_drafts_falls_back_to_tags(router, make):
    router.add(REL, httpx.Response(200, json=[release("v9", draft=True)]))
    router.add(TAGS, httpx.Response(200, json=[]))
    assert await make().fetch("o/r") == []


async def test_404(router, make):
    router.add(REL, httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(TargetNotFound):
        await make().fetch("o/r")


async def test_etag_conditional_request(router, make):
    router.add(
        REL,
        httpx.Response(200, json=[release("v1")], headers={"ETag": 'W/"abc"'}),
        httpx.Response(304, headers={"ETag": 'W/"abc"'}),
    )
    t = make()
    first = await t.fetch("o/r")
    second = await t.fetch("o/r")
    assert first == second and second[0].id == "v1"
    reqs = router.calls(REL)
    assert "if-none-match" not in reqs[0].headers
    assert reqs[1].headers["if-none-match"] == 'W/"abc"'


async def test_token_sent(router, make):
    router.add(REL, httpx.Response(200, json=[]))
    await make(token="sekrit", fallback_to_tags=False).fetch("o/r")
    assert router.requests[0].headers["authorization"] == "Bearer sekrit"


async def test_no_token_no_auth_header(router, make):
    router.add(REL, httpx.Response(200, json=[]))
    await make(fallback_to_tags=False).fetch("o/r")
    assert "authorization" not in router.requests[0].headers


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(403, json={"message": "API rate limit exceeded"},
                       headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 600)}),
        httpx.Response(429, headers={"retry-after": "120"}),
        httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}),
    ],
)
async def test_rate_limit_backs_off(router, make, resp):
    router.add(REL, resp)
    t = make()
    with pytest.raises(RateLimited) as ei:
        await t.fetch("o/r")
    assert ei.value.retry_at > time.time() + 30
    # While backing off, no further requests are made.
    with pytest.raises(RateLimited):
        await t.fetch("o/r")
    assert len(router.requests) == 1


async def test_plain_403_is_not_rate_limit(router, make):
    router.add(REL, httpx.Response(403, json={"message": "Repository access blocked"}))
    with pytest.raises(FetchError):
        await make().fetch("o/r")


@pytest.mark.parametrize(
    "resp",
    [
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("slow"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, text="<html>not json"),
        httpx.Response(200, json={"not": "a list"}),
        httpx.Response(401, json={"message": "Bad credentials"}),
    ],
)
async def test_transient_errors_become_fetch_error(router, make, resp):
    router.add(REL, resp)
    with pytest.raises(FetchError):
        await make().fetch("o/r")


def test_format_message_release():
    t = GitHubReleaseTracker(httpx.AsyncClient())
    v = Version(id="v1.0", name="First <stable>", url="https://github.com/o/r/releases/tag/v1.0",
                notes="## Changes\n\n* **Big** fix & more " + "x" * 1000)
    msg = t.format_message("o/r", v)
    assert '<a href="https://github.com/o/r">o/r</a>' in msg
    assert "<b>First &lt;stable&gt;</b> (<code>v1.0</code>)" in msg
    assert "Big fix &amp; more" in msg and "##" not in msg
    quote = msg.split("<blockquote>")[1].split("</blockquote>")[0]
    assert len(quote) < 560 and quote.endswith("…")
    assert "View release" in msg


def test_format_message_tag_and_pre():
    t = GitHubReleaseTracker(httpx.AsyncClient())
    assert "New tag" in t.format_message("o/r", Version(id="1.0", name="1.0", url="u", label="tag"))
    msg = t.format_message("o/r", Version(id="v2", name="v2", prerelease=True))
    assert "pre-release" in msg and "<code>v2</code>" not in msg and "blockquote" not in msg


def test_excerpt():
    assert excerpt(None) == ""
    assert excerpt("short") == "short"
    assert excerpt("see [docs](http://x) ![img](http://y) <!-- hidden -->") == "see docs"
    long = ("word " * 200).strip()
    out = excerpt(long, 500)
    assert len(out) <= 501 and out.endswith("…") and not out.endswith(" …")


# ------------------------------------------------------------------ automatic Latest → release → tag

BACKPORTS = [release("stable2606-5"), release("stable2609-1"), release("stable2603-8"), release("stable2609-rc2", pre=True)]


async def test_default_follows_github_latest_label(router, make):
    router.add(REL, httpx.Response(200, json=BACKPORTS))
    router.add(LATEST, httpx.Response(200, json=release("stable2609-1")))
    t = make()
    versions = await t.fetch("o/r", [{}])
    assert t.select_latest(versions, {}).id == "stable2609-1"  # not the newer backport stable2606-5
    assert len(router.calls(REL)) == 1 and len(router.calls(LATEST)) == 1


async def test_no_latest_label_falls_back_to_newest_release(router, make):
    router.add(REL, httpx.Response(200, json=[release("v2-rc", pre=True), release("v1")]))
    router.add(LATEST, httpx.Response(404, json={"message": "Not Found"}))
    assert (await make().fetch_latest("o/r")).id == "v1"


async def test_only_prereleases_and_no_latest(router, make):
    router.add(REL, httpx.Response(200, json=[release("v2-rc", pre=True)]))
    assert await make().fetch_latest("o/r") is None


async def test_pre_follows_newest_and_skips_latest_call_when_alone(router, make):
    router.add(REL, httpx.Response(200, json=BACKPORTS))
    t = make()
    versions = await t.fetch("o/r", [{"pre": True}])
    assert t.select_latest(versions, {"pre": True}).id == "stable2606-5"
    assert not router.calls(LATEST)


async def test_mixed_items_share_one_fetch(router, make):
    router.add(REL, httpx.Response(200, json=[release("v3-rc", pre=True), *BACKPORTS]))
    router.add(LATEST, httpx.Response(200, json=release("stable2609-1")))
    t = make()
    versions = await t.fetch("o/r", [{}, {"pre": True}])
    assert t.select_latest(versions, {}).id == "stable2609-1"
    assert t.select_latest(versions, {"pre": True}).id == "v3-rc"
    assert len(router.calls(REL)) == 1 and len(router.calls(LATEST)) == 1


async def test_latest_outside_fetched_page_is_appended(router, make):
    router.add(REL, httpx.Response(200, json=[release("v2-rc", pre=True)]))
    router.add(LATEST, httpx.Response(200, json=release("v1")))
    t = make()
    versions = await t.fetch("o/r", [{}])
    assert [v.id for v in versions] == ["v2-rc", "v1"]
    assert t.select_latest(versions, {}).id == "v1"


async def test_no_releases_uses_tags_without_latest_call(router, make):
    router.add(REL, httpx.Response(200, json=[]))
    router.add(TAGS, httpx.Response(200, json=[{"name": "1.2.0"}]))
    v = await make().fetch_latest("o/r")
    assert v.id == "1.2.0" and v.label == "tag"
    assert not router.calls(LATEST)


async def test_latest_uses_etag(router, make):
    router.add(REL, httpx.Response(200, json=BACKPORTS, headers={"ETag": '"l"'}))
    router.add(
        LATEST,
        httpx.Response(200, json=release("stable2609-1"), headers={"ETag": '"x"'}),
        httpx.Response(304),
    )
    t = make()
    await t.fetch_latest("o/r")
    assert (await t.fetch_latest("o/r")).id == "stable2609-1"
    assert router.calls(LATEST)[1].headers["if-none-match"] == '"x"'


# ------------------------------------------------------------------ --tag

MIXED = [
    release("polkadot-stable2609-rc3", pre=True),
    release("polkadot-stable2606-5"),
    release("polkadot-stable2609-1"),
    release("nightly-2026-11-01", name="Nightly"),
]


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("latest", "polkadot-stable2609-1"),
        ("release", "polkadot-stable2606-5"),
        ("pre-release", "polkadot-stable2609-rc3"),
        ("pre", "polkadot-stable2609-rc3"),
        ("nightly", "nightly-2026-11-01"),          # substring of tag name
        ("polkadot-stable2609*", "polkadot-stable2609-rc3"),  # glob
        ("polkadot-stable2609-[0-9]", "polkadot-stable2609-1"),
        ("doesnotexist", None),
    ],
)
async def test_tag_modes(router, make, tag, expected):
    router.add(REL, httpx.Response(200, json=MIXED))
    router.add(LATEST, httpx.Response(200, json=release("polkadot-stable2609-1")))
    t = make()
    opts = t.parse_options([f"--tag={tag.upper()}"])  # case-insensitive
    got = await t.fetch_latest("o/r", opts)
    assert (got.id if got else None) == expected


async def test_tag_latest_without_latest_badge_is_none(router, make):
    router.add(REL, httpx.Response(200, json=[release("v2-rc", pre=True)]))
    assert await make().fetch_latest("o/r", {"tag": "latest"}) is None


async def test_latest_call_skipped_when_no_item_needs_it(router, make):
    router.add(REL, httpx.Response(200, json=MIXED))
    t = make()
    await t.fetch("o/r", [{"tag": "release"}, {"tag": "nightly"}, {"pre": True}])
    assert not router.calls(LATEST)
    await t.fetch("o/r", [{"tag": "release"}, {"tag": "latest"}])
    assert len(router.calls(LATEST)) == 1


async def test_tag_matches_git_tags_for_repos_without_releases(router, make):
    router.add(REL, httpx.Response(200, json=[]))
    router.add(TAGS, httpx.Response(200, json=[{"name": "v7.4-rc2"}, {"name": "v7.3"}]))
    assert (await make().fetch_latest("o/r", {"tag": "v7.3"})).id == "v7.3"
