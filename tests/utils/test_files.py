import os
from types import SimpleNamespace

from tgforward.utils.files import apply_name_rules, media_filename, sanitize_filename


class TestSanitizeFilename:
    def test_replaces_illegal_chars(self):
        assert sanitize_filename('a<b>c:"d/e\\f|g?h*i') == "a_b_c__d_e_f_g_h_i"

    def test_strips_dots_and_spaces(self):
        assert sanitize_filename("  ..name.. ") == "name"

    def test_truncates_long_names(self):
        assert len(sanitize_filename("x" * 500)) == 250

    def test_empty_fallback(self):
        assert sanitize_filename("...") == "file"


class TestMediaFilename:
    def _msg(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def test_video_name(self):
        m = self._msg(video=SimpleNamespace(file_name="clip.mkv"))
        assert media_filename(m) == "clip.mkv"

    def test_photo_fallback(self):
        m = self._msg(photo=object())
        assert media_filename(m, 123.4) == "123.4.jpg"

    def test_no_media_fallback(self):
        assert media_filename(self._msg(), 7) == "7"


class TestApplyNameRules:
    def test_keeps_extension_and_appends_tag(self, tmp_path):
        p = tmp_path / "show.S01E01.mkv"
        p.write_bytes(b"x")
        out = apply_name_rules(str(p), delete_words=[], replacements={}, rename_tag="@me")
        assert os.path.basename(out) == "show.S01E01 @me.mkv"

    def test_no_forced_mp4(self, tmp_path):
        # 旧实现会把无扩展名文件强制改成 .mp4，现在必须保留原名
        p = tmp_path / "document_v2"
        p.write_bytes(b"x")
        out = apply_name_rules(str(p), delete_words=[], replacements={}, rename_tag="")
        assert out == str(p)

    def test_delete_and_replace(self, tmp_path):
        p = tmp_path / "old_report.zip"
        p.write_bytes(b"x")
        out = apply_name_rules(
            str(p), delete_words=["junk"], replacements={"old": "final"}, rename_tag=""
        )
        assert os.path.basename(out) == "final_report.zip"

    def test_empty_stem_fallback(self, tmp_path):
        p = tmp_path / "SPAM.pdf"
        p.write_bytes(b"x")
        out = apply_name_rules(str(p), delete_words=["SPAM"], replacements={}, rename_tag="")
        assert os.path.basename(out) == "file.pdf"

    def test_existing_target_left_untouched(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x")
        (tmp_path / "b.txt").write_bytes(b"y")
        out = apply_name_rules(
            str(tmp_path / "a.txt"), delete_words=["a"], replacements={"a": "b"}, rename_tag=""
        )
        assert out == str(tmp_path / "b (1).txt")
        assert (tmp_path / "b.txt").read_bytes() == b"y"
