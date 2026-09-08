from dataclasses import replace
from types import SimpleNamespace as NS

from tgforward.runtime import lifecycle
from tgforward.storage.users import UserSettings
from tgforward.transfers import delivery


def key(client=None, settings=None, destination="42", reply=None):
    message = NS(id=1, chat=NS(id=-100123), media_group_id="album", caption="body")
    return delivery.key_for(
        client or NS(me=NS(id=77)),
        message,
        [message],
        settings or UserSettings(42),
        destination,
        reply,
    )


def test_rebuilt_client_same_account_matches():
    assert key(NS(me=NS(id=77))) == key(NS(me=NS(id=77)))
    assert key(NS(me=NS(id=77))) != key(NS(me=NS(id=78)))


def test_non_delivery_preference_does_not_invalidate_recovery():
    settings = UserSettings(42)
    assert key(settings=settings) == key(settings=replace(settings, auto_comments=True))


def test_payload_destination_reply_and_generation_isolate_recovery(monkeypatch):
    monkeypatch.setattr(lifecycle, "_generations", {})
    original = key()
    assert key(settings=UserSettings(42, caption="new")) != original
    assert key(destination="43") != original
    assert key(destination="42/8") != original
    assert key(reply=19) != original
    lifecycle._generations[42] = 1
    assert key() != original


def test_purge_removes_only_selected_user(monkeypatch):
    monkeypatch.setattr(delivery, "_records", delivery.OrderedDict())
    delivery._records.update({"42:1:a": delivery.Record(), "43:1:b": delivery.Record()})
    delivery.purge_user(42)
    assert list(delivery._records) == ["43:1:b"]
