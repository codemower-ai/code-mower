from code_mower.provider_runners.text_schema import (
    clip_text,
    one_line,
    require_exact_keys,
)


def test_clip_text_preserves_short_values() -> None:
    assert clip_text("  useful signal  ", 40) == "useful signal"


def test_clip_text_adds_stable_truncation_marker() -> None:
    assert clip_text("abcdefghij", 9) == "... [truncated]"
    assert clip_text("abcdefghij", 15) == "abcdefghij"


def test_one_line_sanitizes_comment_fragments() -> None:
    assert one_line("line one\nline `two`", 80) == "line one line 'two'"


def test_require_exact_keys_reports_missing_and_extra_keys() -> None:
    assert require_exact_keys({"a": 1, "b": 2}, {"a", "b"}, "item") is None
    assert require_exact_keys({"a": 1}, {"a", "b"}, "item") == (
        "item missing required keys: b"
    )
    assert require_exact_keys({"a": 1, "c": 3}, {"a"}, "item") == (
        "item contains unsupported keys: c"
    )
