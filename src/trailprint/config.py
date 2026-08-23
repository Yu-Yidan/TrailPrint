"""Configuration loading for TrailPrint's deliberately small YAML subset."""

from pathlib import Path
from typing import Any, Dict


DEFAULTS: Dict[str, Any] = {
    "preset": "standard",
    "max_size_mm": 120.0,
    "track_width_mm": 3.0,
    "base_thickness_mm": 1.2,
    "elevation_scale": 0.05,
}


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        raise ValueError("configuration values cannot be empty")
    if (value[0], value[-1]) in (("'", "'"), ('"', '"')):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return float(value) if any(c in value.lower() for c in (".", "e")) else int(value)
    except ValueError:
        return value


def load_flat_yaml(path: Path) -> Dict[str, Any]:
    """Read flat `key: value` YAML; nested structures are not supported."""
    result: Dict[str, Any] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"{path}:{number}: expected 'key: value'")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key in result:
            raise ValueError(f"{path}:{number}: invalid or duplicate key")
        result[key] = _parse_scalar(value)
    return result


def load_config(print_dir: Path, presets_dir: Path) -> Dict[str, Any]:
    local_path = print_dir / "print.yaml"
    if not local_path.is_file():
        raise ValueError(f"missing configuration: {local_path}")
    local = load_flat_yaml(local_path)
    preset_name = str(local.get("preset", DEFAULTS["preset"]))
    preset_path = presets_dir / f"{preset_name}.yaml"
    if not preset_path.is_file():
        raise ValueError(f"unknown preset '{preset_name}'")

    config = {**DEFAULTS, **load_flat_yaml(preset_path), **local}
    for key in ("max_size_mm", "track_width_mm", "base_thickness_mm"):
        if not isinstance(config[key], (int, float)) or config[key] <= 0:
            raise ValueError(f"{key} must be a positive number")
    if not isinstance(config["elevation_scale"], (int, float)) or config["elevation_scale"] < 0:
        raise ValueError("elevation_scale must be a non-negative number")
    return config
