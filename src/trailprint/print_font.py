"""Prepare a private Arial Bold copy with the print-tested comma tail.

The installed font and glyph advances are never changed. Keep this outside the
Blender worker: fontTools and Shapely belong to the application's environment.
"""

from copy import deepcopy
from pathlib import Path

import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from shapely.geometry import LineString, Polygon, box
from shapely.geometry.polygon import orient

from .fonts import MODEL_FONT_PATH, require_model_font

# The approved coupon had a 0.772 mm square head and a 0.600 mm tail shaft.
# Ratios keep that design at every text size, without changing its length.
REFERENCE_HEAD_MM = 0.772
TAIL_WIDTH_MM = 0.600


class _OutlinePen(BasePen):
    def __init__(self, glyphs):
        super().__init__(glyphs)
        self.rings = []
        self.points = []

    def _moveTo(self, point):
        self.points = [point]

    def _lineTo(self, point):
        self.points.append(point)

    def _qCurveToOne(self, control, end):
        start = np.asarray(self.points[-1])
        for t in np.linspace(0, 1, 65)[1:]:
            self.points.append(tuple(
                (1-t)**2*start + 2*(1-t)*t*np.asarray(control) + t*t*np.asarray(end)))

    def _curveToOne(self, first, second, end):
        start = np.asarray(self.points[-1])
        for t in np.linspace(0, 1, 65)[1:]:
            self.points.append(tuple(
                (1-t)**3*start + 3*(1-t)**2*t*np.asarray(first)
                + 3*(1-t)*t*t*np.asarray(second) + t**3*np.asarray(end)))

    def _closePath(self):
        self.rings.append(self.points)
        self.points = []


def glyph_outline(font, name):
    """Return an unhinted outline in font units, including composite transforms."""
    glyphs = font.getGlyphSet()
    pen = _OutlinePen(glyphs)
    glyphs[name].draw(pen)
    outline = Polygon()
    for ring in pen.rings:
        outline = outline.symmetric_difference(Polygon(ring))
    return outline


def thicker_comma(outline, head_height):
    """Reproduce the middle coupon in any uniform coordinate scale."""
    minx, miny, maxx, maxy = outline.bounds
    scale = head_height / REFERENCE_HEAD_MM
    head_bottom = maxy - head_height
    if (not outline.is_valid or outline.geom_type != "Polygon"
            or not miny < head_bottom < maxy or head_height <= 0):
        raise ValueError("Arial Bold comma has an unexpected outline.")
    points = []
    for y in np.linspace(miny + .06*scale, head_bottom + .15*scale, 32):
        section = outline.intersection(LineString([(minx-scale, y), (maxx+scale, y)]))
        if section.is_empty:
            raise ValueError("Could not locate the Arial Bold comma tail.")
        points.append(((section.bounds[0]+section.bounds[2])/2, y))
    tail = LineString(points).buffer(TAIL_WIDTH_MM*scale/2, quad_segs=16)
    tail = tail.intersection(box(minx-scale, miny, maxx+scale, head_bottom))
    result = outline.union(tail)
    if not result.is_valid or result.geom_type != "Polygon":
        raise ValueError("Could not construct the thicker comma tail.")
    return result


def prepare_print_font(destination: Path, source=MODEL_FONT_PATH) -> Path:
    """Write a build-local font; freeze glyphs that reuse the original comma."""
    source = require_model_font(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("The print font must not replace the installed font.")
    with TTFont(source, recalcTimestamp=False) as font:
        name = font.getBestCmap().get(ord(","))
        if name is None or "glyf" not in font:
            raise ValueError("Arial Bold is missing its TrueType comma outline.")
        original = glyph_outline(font, name)
        # Arial Bold's comma has a square head, from baseline to its top.
        head_height = original.bounds[3]
        corrected = thicker_comma(original, head_height)
        # Apostrophes and other composites reuse 'comma' in the installed font.
        # Resolve their original geometry before replacing the comma itself.
        original_name = name + ".trailprint-original"
        if original_name in font.getGlyphOrder():
            raise ValueError("The source font already contains a print comma correction.")
        order = list(font.getGlyphOrder())
        font["glyf"][original_name] = deepcopy(font["glyf"][name])
        font["hmtx"][original_name] = font["hmtx"][name]
        font.setGlyphOrder([*order, original_name])
        for other in font.getGlyphOrder():
            glyph = font["glyf"][other]
            if glyph.isComposite():
                for component in glyph.components:
                    if component.glyphName == name:
                        component.glyphName = original_name
        pen = TTGlyphPen(None)
        # TrueType uses clockwise outer contours and counterclockwise holes.
        corrected = orient(corrected, sign=-1)
        for ring in (corrected.exterior, *corrected.interiors):
            points = []
            for x, y in ring.coords:
                point = (round(x), round(y))
                if not points or point != points[-1]:
                    points.append(point)
            if points[-1] == points[0]:
                points.pop()
            pen.moveTo(points[0])
            for point in points[1:]:
                pen.lineTo(point)
            pen.closePath()
        glyph = pen.glyph()
        font["glyf"][name] = glyph
        glyph.recalcBounds(font["glyf"])
        # Reflect the widened tail's left extent without shifting the head.
        # The advance (and therefore every following character) stays fixed.
        advance, _ = font["hmtx"][name]
        font["hmtx"][name] = (advance, glyph.xMin)
        destination.parent.mkdir(parents=True, exist_ok=True)
        font.save(destination)
    return destination
