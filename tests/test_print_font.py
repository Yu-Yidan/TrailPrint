import hashlib
import tempfile
from pathlib import Path

import pytest
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont
from shapely import affinity
from shapely.geometry import box

from trailprint.fonts import MODEL_FONT_PATH
from trailprint.print_font import glyph_outline, prepare_print_font, thicker_comma


def test_only_comma_outline_changes_and_advance_is_preserved():
    original_hash = hashlib.sha256(MODEL_FONT_PATH.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as temp:
        path = prepare_print_font(Path(temp) / "print.ttf")
        with TTFont(MODEL_FONT_PATH) as original, TTFont(path) as repaired:
            assert original.getBestCmap() == repaired.getBestCmap()
            assert original.getGlyphOrder() == repaired.getGlyphOrder()[:-1]
            assert original["hhea"].ascent == repaired["hhea"].ascent
            assert original["hhea"].descent == repaired["hhea"].descent
            assert original["head"].unitsPerEm == repaired["head"].unitsPerEm
            comma = original.getBestCmap()[ord(",")]
            before, after = original.getGlyphSet(), repaired.getGlyphSet()
            for name in original.getGlyphOrder():
                assert original["hmtx"][name][0] == repaired["hmtx"][name][0]
                if name == comma:
                    continue
                assert original["hmtx"][name] == repaired["hmtx"][name]
                first = DecomposingRecordingPen(before)
                second = DecomposingRecordingPen(after)
                before[name].draw(first)
                after[name].draw(second)
                assert first.value == second.value, name
            first = glyph_outline(original, comma)
            second = glyph_outline(repaired, comma)
            head = box(-1000, 1, 2000, 2000)  # exclude the tail's shared baseline
            assert first.intersection(head).hausdorff_distance(second.intersection(head)) < 1
            assert first.bounds[1::2] == second.bounds[1::2]
            assert second.area > first.area
            expected = thicker_comma(first, first.bounds[3])
            assert expected.hausdorff_distance(second) < 1  # font-unit rounding
            # At the coupon size, outline differences stay well below 0.01 mm.
            factor = .772 / first.bounds[3]
            assert affinity.scale(expected, factor, factor, origin=(0, 0)).hausdorff_distance(
                affinity.scale(second, factor, factor, origin=(0, 0))) < .01
    assert hashlib.sha256(MODEL_FONT_PATH.read_bytes()).hexdigest() == original_hash


def test_font_preparation_does_not_replace_source_or_substitute_missing_font(tmp_path):
    with pytest.raises(ValueError, match="must not replace"):
        prepare_print_font(MODEL_FONT_PATH)
    with pytest.raises(ValueError, match="Model font was not found"):
        prepare_print_font(tmp_path / "output.ttf", tmp_path / "missing.ttf")
