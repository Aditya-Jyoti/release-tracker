import pytest

from bot.checker import Checker, detect_change
from bot.db import Database, TrackedItem
from bot.trackers import FetchError, RateLimited, Tracker, TrackerRegistry, Version


class FakeTracker(Tracker):
    kind = "fake"
    flags = {"--pre": "pre"}

    def __init__(self):
        self.data: dict[str, list[Version] | Exception] = {}
        self.fetches: list[str] = []
        self.requested: dict[str, list] = {}

    def normalize(self, arg):
        return arg

    async def fetch(self, target, requested=()):
        self.fetches.append(target)
        self.requested[target] = list(requested)
        v = self.data[target]
        if isinstance(v, Exception):
            raise v
        return v

    def select_latest(self, versions, options):
        return next((v for v in versions if options.get("pre") or not v.prerelease), None)

    def format_message(self, target, version):
        return f"{target}:{version.id}"


class Outbox:
    def __init__(self, result=True):
        self.sent: list[tuple[int, str]] = []
        self.result = result

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))
        return self.result(chat_id) if callable(self.result) else self.result


@pytest.fixture
def env():
    db = Database(":memory:")
    tracker = FakeTracker()
    checker = Checker(db, TrackerRegistry([tracker]))
    yield db, tracker, checker
    db.close()


def v(id, pre=False):
    return Version(id=id, name=id, prerelease=pre)


def item(last):
    return TrackedItem(1, 1, "fake", "t", {}, last, None, None, "")


def test_detect_change():
    assert detect_change(item("v1"), v("v2"))
    assert not detect_change(item("v1"), v("v1"))
    assert not detect_change(item("v1"), None)
    assert not detect_change(item(None), None)
    assert detect_change(item(None), v("v1"))


async def test_no_change_no_notification(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, "v1")
    tracker.data["a"] = [v("v1")]
    out = Outbox()
    report = await checker.run(out)
    assert out.sent == [] and report.notifications == 0
    assert db.get_item(1, "fake", "a").last_version == "v1"


async def test_new_version_notifies_and_persists(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, "v1")
    tracker.data["a"] = [v("v2"), v("v1")]
    out = Outbox()
    report = await checker.run(out)
    assert out.sent == [(1, "a:v2")] and report.notifications == 1
    assert db.get_item(1, "fake", "a").last_version == "v2"
    # Second run: nothing new.
    out2 = Outbox()
    await checker.run(out2)
    assert out2.sent == []


async def test_fetches_each_target_once_across_chats(env):
    db, tracker, checker = env
    for chat in (1, 2, 3):
        db.upsert_item(chat, "fake", "shared", {}, "v1")
    db.upsert_item(1, "fake", "other", {}, "x1")
    tracker.data["shared"] = [v("v2")]
    tracker.data["other"] = [v("x1")]
    out = Outbox()
    report = await checker.run(out)
    assert sorted(tracker.fetches) == ["other", "shared"]
    assert sorted(out.sent) == [(1, "shared:v2"), (2, "shared:v2"), (3, "shared:v2")]
    assert report.targets == 2 and report.items == 4


async def test_per_item_prerelease_option(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, "v1")
    db.upsert_item(2, "fake", "a", {"pre": True}, "v1")
    tracker.data["a"] = [v("v2-rc1", pre=True), v("v1")]
    out = Outbox()
    await checker.run(out)
    assert out.sent == [(2, "a:v2-rc1")]
    assert len(tracker.fetches) == 1


async def test_errors_are_isolated(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "broken", {}, "v1")
    db.upsert_item(1, "fake", "limited", {}, "v1")
    db.upsert_item(1, "fake", "crashy", {}, "v1")
    db.upsert_item(1, "fake", "ok", {}, "v1")
    tracker.data["broken"] = FetchError("network down")
    tracker.data["limited"] = RateLimited("rate limit", 0)
    tracker.data["crashy"] = RuntimeError("bug")
    tracker.data["ok"] = [v("v2")]
    out = Outbox()
    report = await checker.run(out)
    assert out.sent == [(1, "ok:v2")]
    assert set(report.errors) == {("fake", "broken"), ("fake", "limited"), ("fake", "crashy")}
    assert report.rate_limited
    assert db.get_item(1, "fake", "broken").last_version == "v1"


async def test_unknown_kind_is_reported_not_raised(env):
    db, tracker, checker = env
    db.upsert_item(1, "removed-kind", "x", {}, "v1")
    report = await checker.run(Outbox())
    assert ("removed-kind", "x") in report.errors


async def test_transient_send_failure_retries_next_cycle(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, "v1")
    tracker.data["a"] = [v("v2")]
    await checker.run(Outbox(result=False))
    assert db.get_item(1, "fake", "a").last_version == "v1"
    out = Outbox()
    await checker.run(out)
    assert out.sent == [(1, "a:v2")]
    assert db.get_item(1, "fake", "a").last_version == "v2"


async def test_one_chat_failing_does_not_block_others(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, "v1")
    db.upsert_item(2, "fake", "a", {}, "v1")
    tracker.data["a"] = [v("v2")]

    async def send(chat_id, text):
        if chat_id == 1:
            raise RuntimeError("unexpected")
        return True

    await checker.run(send)
    assert db.get_item(1, "fake", "a").last_version == "v1"
    assert db.get_item(2, "fake", "a").last_version == "v2"


async def test_first_release_after_empty_baseline(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, None)
    tracker.data["a"] = []
    out = Outbox()
    await checker.run(out)
    assert out.sent == []
    tracker.data["a"] = [v("v1")]
    await checker.run(out)
    assert out.sent == [(1, "a:v1")]


async def test_empty_db(env):
    _, tracker, checker = env
    report = await checker.run(Outbox())
    assert report.targets == 0 and tracker.fetches == []


async def test_fetch_receives_all_item_options(env):
    db, tracker, checker = env
    db.upsert_item(1, "fake", "a", {}, "v1")
    db.upsert_item(2, "fake", "a", {"pre": True}, "v1")
    tracker.data["a"] = [v("v1")]
    await checker.run(Outbox())
    assert sorted(tracker.requested["a"], key=len) == [{}, {"pre": True}]
