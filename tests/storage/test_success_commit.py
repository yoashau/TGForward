import asyncio

import pytest

from tgforward.runtime import lifecycle
from tgforward.storage import users


def test_atomic_success_is_idempotent_and_does_not_create_missing_user(sqlite_store):
    async def run():
        args = (942, "source", 1, False, "done", "task:unit:message")
        assert await users.record_extract_success(*args) == "user_missing"
        assert await users.get_user(942) is None
        await users.set_field(942, "caption", "existing")
        assert await users.record_extract_success(*args) == "committed"
        assert await users.record_extract_success(*args) == "already_committed"
        doc = await users.get_user(942)
        assert doc["stats"]["extracts"] == 1 and len(doc["history"]) == 1
        assert doc["recent_commit_keys"] == [args[-1]]
        assert doc["caption"] == "existing"

    asyncio.run(run())


def test_committed_then_cancelled_retry_does_not_double_count(sqlite_store, monkeypatch):
    original = users.storage.mutate_user

    async def run():
        await users.set_field(942, "caption", "existing")

        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()

        args = (942, "source", 1, False, "done", "task:unit:message")
        monkeypatch.setattr(users.storage, "mutate_user", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await users.record_extract_success(*args)
        monkeypatch.setattr(users.storage, "mutate_user", original)
        assert await users.record_extract_success(*args) == "already_committed"
        doc = await users.get_user(942)
        assert doc["stats"]["extracts"] == 1 and len(doc["history"]) == 1

    asyncio.run(run())


def test_success_keys_bounded_and_stale_permit_rejected(sqlite_store, monkeypatch):
    monkeypatch.setattr(users, "RECENT_COMMIT_LIMIT", 3)
    monkeypatch.setattr(lifecycle, "revoked", set())
    monkeypatch.setattr(lifecycle, "_generations", {})

    async def run():
        await users.set_field(942, "caption", "existing")
        permit = lifecycle.capture_permit(942)
        for i in range(8):
            await users.record_extract_success(942, "s", i, False, "done", str(i), permit=permit)
        doc = await users.get_user(942)
        assert doc["recent_commit_keys"] == ["5", "6", "7"]
        lifecycle.revoke(942)
        with pytest.raises(lifecycle.StalePermit):
            await users.record_extract_success(942, "s", 9, False, "done", "9", permit=permit)
        assert await users.get_user(942) == doc

    asyncio.run(run())
