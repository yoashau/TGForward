from tgforward.utils.links import find_links, parse_batch_count, parse_link


class TestParseLink:
    def test_public_simple(self):
        ref = parse_link("https://t.me/channelname/100")
        assert ref.chat == "channelname"
        assert ref.message_id == 100
        assert ref.is_private is False

    def test_public_topic(self):
        ref = parse_link("https://t.me/groupname/5/789")
        assert ref.chat == "groupname"
        assert ref.message_id == 789
        assert ref.is_private is False

    def test_private_simple(self):
        ref = parse_link("https://t.me/c/1234567/456")
        assert ref.chat == "-1001234567"
        assert ref.message_id == 456
        assert ref.is_private is True

    def test_private_topic(self):
        ref = parse_link("https://t.me/c/1234567/5/789")
        assert ref.chat == "-1001234567"
        assert ref.message_id == 789
        assert ref.is_private is True

    def test_scheme_and_host_variants(self):
        for url in (
            "http://t.me/c/123/456",
            "https://telegram.me/foo/12",
            "https://www.t.me/foo/12",
            "https://t.me/foo/12?single",
        ):
            ref = parse_link(url)
            assert ref is not None, url

    def test_invalid(self):
        assert parse_link("https://t.me/channelname") is None
        assert parse_link("https://example.com/foo/1") is None
        assert parse_link("not a link") is None


class TestFindLinks:
    def test_finds_all_and_keeps_order(self):
        text = "看这些：\nhttps://t.me/c/123/789\nhttps://t.me/foo/10"
        urls = find_links(text)
        assert len(urls) == 2
        assert "/c/123/789" in urls[0]
        assert "/foo/10" in urls[1]

    def test_dedup_same_target(self):
        text = "https://t.me/foo/10 https://www.t.me/foo/10?single"
        assert len(find_links(text)) == 1

    def test_plain_text_no_links(self):
        assert find_links("随便聊聊，没有链接") == []


class TestParseBatchCount:
    def test_with_count(self):
        text = "https://t.me/foo/100 5"
        assert parse_batch_count(text, "https://t.me/foo/100") == 5

    def test_without_count(self):
        text = "帮我提取 https://t.me/foo/100 谢谢"
        assert parse_batch_count(text, "https://t.me/foo/100") == 1

    def test_count_with_query(self):
        text = "https://t.me/foo/10?single 4"
        assert parse_batch_count(text, "https://t.me/foo/10") == 4

    def test_not_confused_by_longer_id(self):
        # /105 中的 "10" 不能被误当成 "https://t.me/x/10" 的批量数量
        text = "https://t.me/x/105 https://t.me/x/10 开始"
        assert parse_batch_count(text, "https://t.me/x/10") == 1

    def test_capped(self):
        text = "https://t.me/foo/1 999999"
        assert parse_batch_count(text, "https://t.me/foo/1") == 10_000
