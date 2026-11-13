import pytest

from bot.db import MIGRATIONS, Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def test_migrations_applied(db):
    assert db.schema_version == len(MIGRATIONS)


def test_file_db_persists_and_creates_parent(tmp_path):
    path = tmp_path / "nested" / "dir" / "t.db"
    d = Database(path)
    d.upsert_item(1, "github", "o/r", {"pre": True}, "v1")
    d.close()
    d2 = Database(path)  # re-open: migrations are idempotent
    item = d2.get_item(1, "github", "o/r")
    assert item.options == {"pre": True} and item.last_version == "v1"
    d2.close()


def test_upsert_creates_then_updates(db):
    item, created = db.upsert_item(1, "github", "o/r", {}, "v1")
    assert created and item.last_version == "v1" and item.options == {} and item.created_at
    item2, created2 = db.upsert_item(1, "github", "o/r", {"pre": True}, "v2-rc")
    assert not created2 and item2.id == item.id
    assert item2.options == {"pre": True} and item2.last_version == "v2-rc"
    assert len(db.list_items()) == 1


def test_same_target_different_chats_and_kinds(db):
    db.upsert_item(1, "github", "o/r", {}, "v1")
    db.upsert_item(2, "github", "o/r", {}, "v1")
    db.upsert_item(1, "docker", "o/r", {}, "latest")
    assert len(db.list_items()) == 3
    assert [i.kind for i in db.list_items(1)] == ["docker", "github"]
    assert db.distinct_targets() == [("docker", "o/r"), ("github", "o/r")]


def test_null_baseline(db):
    item, _ = db.upsert_item(1, "github", "o/r", {}, None)
    assert item.last_version is None


def test_remove(db):
    db.upsert_item(1, "github", "o/r", {}, "v1")
    db.upsert_item(2, "github", "o/r", {}, "v1")
    assert db.remove_item(1, "github", "o/r")
    assert not db.remove_item(1, "github", "o/r")
    assert db.get_item(1, "github", "o/r") is None
    assert db.get_item(2, "github", "o/r") is not None


def test_set_version_and_mark_checked(db):
    item, _ = db.upsert_item(1, "github", "o/r", {}, "v1")
    db.set_version(item.id, "v2")
    updated = db.get_item(1, "github", "o/r")
    assert updated.last_version == "v2" and updated.last_changed_at is not None
    db.mark_checked([item.id])
    db.mark_checked([])
    assert db.get_item(1, "github", "o/r").last_checked_at is not None


def test_list_items_empty(db):
    assert db.list_items() == [] and db.list_items(42) == []
