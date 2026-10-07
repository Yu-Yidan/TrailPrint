"""Shared model-font settings; safe to import from Blender's Python."""

from pathlib import Path

MODEL_FONT_NAME = "Arial Bold"
MODEL_FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
MODEL_FONT_BROWSER_CSS = "bold 1000px Arial, sans-serif"
# Unscaled Name height measured in Blender 5.2 with the installed font.
BLENDER_FONT_REFERENCE_HEIGHT = 0.5246478319168091


def require_model_font(path=MODEL_FONT_PATH):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Model font was not found: {path}")
    return path
