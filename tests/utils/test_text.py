from tgforward.utils.text import apply_text_rules, split_text


class TestApplyTextRules:
    def test_replacements(self):
        assert apply_text_rules("hello Team SPY", {"Team SPY": "我的频道"}, []) == "hello 我的频道"

    def test_delete_words(self):
        assert apply_text_rules("foo bar baz", {}, ["bar"]) == "foo  baz"

    def test_replace_then_delete(self):
        out = apply_text_rules("aaa bbb", {"aaa": "xxx"}, ["bbb"])
        assert out == "xxx "

    def test_delete_keeps_newlines(self):
        text = "第一行\n第二行 水印 内容\n第三行"
        out = apply_text_rules(text, {}, ["水印"])
        assert out == "第一行\n第二行  内容\n第三行"

    def test_delete_mid_sentence_chinese(self):
        # 与替换规则一致的子串删除：中文无词边界也能删
        assert apply_text_rules("这是一个水印测试", {}, ["水印"]) == "这是一个测试"

    def test_empty_text(self):
        assert apply_text_rules(None, {"a": "b"}, ["c"]) == ""

    def test_no_rules(self):
        assert apply_text_rules("原样返回", None, None) == "原样返回"


class TestSplitText:
    def test_short_text_single_chunk(self):
        assert split_text("hello") == ["hello"]

    def test_empty(self):
        assert split_text("") == [""]

    def test_hard_cut_without_newlines(self):
        chunks = split_text("a" * 10_000, 4096)
        assert len(chunks) == 3
        assert all(len(c) <= 4096 for c in chunks)
        assert "".join(chunks) == "a" * 10_000

    def test_prefers_newline_breaks(self):
        text = ("短行\n" * 3000)[:-1]
        chunks = split_text(text, 100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text

    def test_multibyte_content_split(self):
        text = "段落一\n\n" + "很长的内容。" * 1000
        chunks = split_text(text, 4096)
        assert len(chunks) > 1
        assert all(len(c) <= 4096 for c in chunks)
        assert chunks[0].startswith("段落一")
