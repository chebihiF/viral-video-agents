"""Sous-titres ASS mot par mot (#52) : calcul pur, sans ffmpeg."""

import re
from pathlib import Path

import pytest

from media_service.subtitles import ShotText, SubtitleError, build_ass, write_ass

_STAMP = r"(\d+:\d\d:\d\d\.\d\d)"
_DIALOGUE = re.compile(rf"^Dialogue: 0,{_STAMP},{_STAMP},Word,,0,0,0,,(.*)$")


def _seconds(stamp: str) -> float:
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _events(ass: str) -> list[tuple[float, float, str]]:
    found = [_DIALOGUE.match(line) for line in ass.splitlines() if line.startswith("Dialogue:")]
    return [(_seconds(m.group(1)), _seconds(m.group(2)), m.group(3)) for m in found if m]


def test_one_event_per_word_covering_the_shot_without_overlap() -> None:
    events = _events(build_ass([ShotText("un petit chat", 3.0)]))
    assert [text for _, _, text in events] == ["un", "petit", "chat"]
    assert events[0][0] == 0.0
    assert events[-1][1] == 3.0
    for (_, end, _), (start, _, _) in zip(events, events[1:], strict=False):
        assert end == start


def test_word_duration_is_proportional_to_word_length() -> None:
    (a_start, a_end, _), (b_start, b_end, _) = _events(build_ass([ShotText("ab abcdef", 4.0)]))
    assert (a_end - a_start) == pytest.approx(1.0, abs=0.02)
    assert (b_end - b_start) == pytest.approx(3.0, abs=0.02)


def test_second_shot_starts_after_first_duration() -> None:
    events = _events(build_ass([ShotText("un deux", 2.0), ShotText("trois quatre", 3.0)]))
    assert [text for _, _, text in events] == ["un", "deux", "trois", "quatre"]
    assert events[1][1] == 2.0
    assert events[2][0] == 2.0
    assert events[-1][1] == 5.0


def test_shot_without_words_still_shifts_following_shots() -> None:
    events = _events(build_ass([ShotText("  ", 2.0), ShotText("salut", 1.0)]))
    assert events == [(2.0, 3.0, "salut")]


def test_arabic_words_are_kept_and_file_is_utf8(tmp_path: Path) -> None:
    text = "القط الصغير يطير"
    target = tmp_path / "sub.ass"
    write_ass([ShotText(text, 3.0)], target)
    content = target.read_bytes().decode("utf-8")
    assert [t for _, _, t in _events(content)] == text.split()


def test_header_declares_portrait_1080x1920_and_unicode_font() -> None:
    ass = build_ass([ShotText("mot", 1.0)])
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "Style: Word,DejaVu Sans," in ass


def test_markup_is_neutralised() -> None:
    events = _events(build_ass([ShotText(r"{\an8}a \N b", 1.0)]))
    assert all("{" not in t and "}" not in t and "\\" not in t for _, _, t in events)


@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_duration_is_refused(duration: float) -> None:
    with pytest.raises(SubtitleError):
        build_ass([ShotText("mot", duration)])


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "sub.ass"
    write_ass([ShotText("mot", 1.0)], target)
    assert [p.name for p in tmp_path.iterdir()] == ["sub.ass"]


def test_each_word_lasts_at_least_one_centisecond() -> None:
    events = _events(build_ass([ShotText("a bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb c", 0.03)]))
    assert len(events) == 3
    assert all(round((end - start) * 100) >= 1 for start, end, _ in events)
    assert events[-1][1] == 0.03


def test_too_many_words_for_the_duration_is_refused() -> None:
    with pytest.raises(SubtitleError):
        build_ass([ShotText("a b c", 0.02)])


def test_mixed_arabic_latin_with_punctuation_is_kept() -> None:
    text = "مرحبا، hello! القط (cat) يطير؟"
    events = _events(build_ass([ShotText(text, 4.0)]))
    assert [t for _, _, t in events] == text.split()
