import httpx
import pytest

from bot.trackers import InvalidTarget, TrackerRegistry
from bot.trackers.github_release import GitHubReleaseTracker, parse_repo


@pytest.mark.parametrize(
    "arg",
    [
        "python/cpython",
        "Python/CPython",
        "  python/cpython  ",
        "https://github.com/python/cpython",
        "http://github.com/python/cpython/",
        "https://www.github.com/python/cpython",
        "github.com/python/cpython",
        "https://github.com/python/cpython.git",
        "https://github.com/python/cpython/releases",
        "https://github.com/python/cpython/releases/tag/v3.13.0",
        "https://github.com/python/cpython?tab=readme",
        "https://github.com/python/cpython#readme",
        "git@github.com:python/cpython.git",
        "<https://github.com/python/cpython>",
    ],
)
def test_parse_repo_accepts(arg):
    assert parse_repo(arg) == "python/cpython"


def test_parse_repo_keeps_dots_and_dashes():
    assert parse_repo("vercel/next.js") == "vercel/next.js"
    assert parse_repo("some-org/my_repo-2") == "some-org/my_repo-2"


@pytest.mark.parametrize(
    "arg",
    [
        "",
        "cpython",
        "python/cpython/extra",
        "https://gitlab.com/python/cpython",
        "https://github.com/python",
        "-bad/repo",
        "owner/..",
        "own er/repo",
        "a" * 40 + "/repo",
        "owner/re$po",
    ],
)
def test_parse_repo_rejects(arg):
    with pytest.raises(InvalidTarget):
        parse_repo(arg)


@pytest.fixture
def registry():
    return TrackerRegistry([GitHubReleaseTracker(httpx.AsyncClient())])


def test_registry_default_kind(registry):
    p = registry.parse_args(["owner/repo"])
    assert p.tracker.kind == "github" and p.target == "owner/repo" and p.flags == ()


def test_registry_explicit_kind_and_flags(registry):
    p = registry.parse_args(["github", "owner/repo", "--PRE"])
    assert p.tracker.kind == "github" and p.target == "owner/repo" and p.flags == ("--pre",)


def test_registry_flags_anywhere(registry):
    assert registry.parse_args(["--pre", "owner/repo"]).flags == ("--pre",)


@pytest.mark.parametrize("args", [[], ["--pre"], ["a/b", "c/d"], ["nope", "a/b"]])
def test_registry_bad_args(registry, args):
    with pytest.raises(InvalidTarget):
        registry.parse_args(args)


def test_parse_options(registry):
    t = registry.get("github")
    assert t.parse_options([]) == {}
    assert t.parse_options(["--pre"]) == {"pre": True}
    with pytest.raises(InvalidTarget, match="Unknown option"):
        t.parse_options(["--bogus"])


def test_registry_rejects_duplicates():
    c = httpx.AsyncClient()
    with pytest.raises(ValueError):
        TrackerRegistry([GitHubReleaseTracker(c), GitHubReleaseTracker(c)])


@pytest.mark.parametrize(
    "args",
    [
        ["owner/repo", "--tag", "Latest"],
        ["owner/repo", "--tag=LATEST"],
        ["--TAG", "latest", "owner/repo"],
        ["owner/repo", "—tag", "latest"],  # Telegram autocorrected -- into an em dash
        ["owner/repo", "–tag=latest"],     # ... or an en dash
        ["github", "owner/repo", "--tag", "latest"],
    ],
)
def test_tag_value_flag(registry, args):
    p = registry.parse_args(args)
    assert p.target == "owner/repo"
    assert p.tracker.parse_options(p.flags) == {"tag": "latest"}


def test_tag_with_pre_and_errors(registry):
    t = registry.get("github")
    p = registry.parse_args(["owner/repo", "--tag", "polkadot-stable*", "--pre"])
    with pytest.raises(InvalidTarget, match="can't be combined"):
        t.parse_options(p.flags)
    with pytest.raises(InvalidTarget, match="needs a value"):
        t.parse_options(registry.parse_args(["owner/repo", "--tag"]).flags)
    with pytest.raises(InvalidTarget, match="Unknown option"):
        t.parse_options(["--pre=yes"])
    # a value flag followed by another flag doesn't swallow it
    assert registry.parse_args(["owner/repo", "--tag", "--pre"]).flags == ("--tag", "--pre")
