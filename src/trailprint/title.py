"""Resolve printable project titles and validate their physical plate fit."""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import ImageFont


FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Black.ttf")
TEXT_HEIGHT_MM = 5.0
TITLE_LIMITS_MM = {
    "HEXAGON": {"INNER TEXT": 45.0, "OUTER TEXT": 60.5, "FRONT TEXT": 60.5},
    "OCTAGON": {"OUTER TEXT": 41.3},
    "CIRCLE": {"OUTER TEXT": 77.8},
}


def readable_project_name(slug: str) -> str:
    return slug.replace("-", " ").title()


def resolve_project_title(value: str, slug: str) -> str:
    return str(value).replace("{name}", readable_project_name(slug)).strip()


@lru_cache(maxsize=1)
def _font():
    if not FONT_PATH.is_file():
        raise ValueError(f"Title font was not found: {FONT_PATH}")
    return ImageFont.truetype(str(FONT_PATH), 1000)


def measure_title_width_mm(value: str) -> float:
    """Match EmGi's Arial Black width after scaling its `Name` placeholder to 5 mm."""
    font = _font()
    reference = font.getbbox("Name")
    reference_height = reference[3] - reference[1]
    if reference_height <= 0:
        raise ValueError("Could not measure the title font.")
    return float(font.getlength(str(value))) / reference_height * TEXT_HEIGHT_MM


def title_limit_mm(shape: str, shape_extra: str) -> Optional[float]:
    return TITLE_LIMITS_MM.get(str(shape).upper(), {}).get(str(shape_extra).upper())


def validate_title_fit(value: str, shape: str, shape_extra: str) -> float:
    limit = title_limit_mm(shape, shape_extra)
    if limit is None:
        return 0.0
    width = measure_title_width_mm(value)
    if width > limit:
        raise ValueError(
            f'Title is approximately {width:.1f} mm wide, but {shape.title()} '
            f'{shape_extra.title()} allows {limit:.1f} mm. Shorten the title before building.')
    return width
