from tgforward.storage.users import UserSettings
from tgforward.transfers.transfer import Destination, _media_caption, _resolve_target


class TestResolveTarget:
    def test_default_is_user_chat(self):
        s = UserSettings(user_id=1)
        assert _resolve_target(s, "6414886697") == Destination(6414886697)

    def test_channel_id(self):
        s = UserSettings(user_id=1, chat_id="-1001234567890")
        assert _resolve_target(s, "1") == Destination(-1001234567890)

    def test_channel_with_topic(self):
        s = UserSettings(user_id=1, chat_id="-1001234567890/12")
        assert _resolve_target(s, "1") == Destination(-1001234567890, 12)

    def test_invalid_falls_back_to_user_chat(self):
        s = UserSettings(user_id=1, chat_id="not-a-chat")
        assert _resolve_target(s, "42") == Destination(42)

    def test_partial_topic_falls_back(self):
        s = UserSettings(user_id=1, chat_id="-100123/abc")
        assert _resolve_target(s, "42") == Destination(42)


class TestMediaCaption:
    def test_none_and_empty(self):
        assert _media_caption(None) is None
        assert _media_caption("") is None

    def test_within_limit(self):
        assert _media_caption("hi") == "hi"

    def test_truncated_to_1024(self):
        out = _media_caption("y" * 2000)
        assert len(out) == 1024
        assert out.endswith("…")
