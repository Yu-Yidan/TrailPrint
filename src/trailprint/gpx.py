"""GPX parsing and geographic projection."""

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

Point3 = Tuple[float, float, float]


def read_gpx(path: Path) -> List[Point3]:
    if not path.is_file():
        raise ValueError(f"missing GPX input: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"invalid GPX XML: {exc}") from exc

    points: List[Point3] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "trkpt":
            continue
        try:
            lat, lon = float(node.attrib["lat"]), float(node.attrib["lon"])
        except (KeyError, ValueError) as exc:
            raise ValueError("track point has invalid latitude or longitude") from exc
        elevation = 0.0
        for child in node:
            if child.tag.rsplit("}", 1)[-1] == "ele" and child.text:
                try:
                    elevation = float(child.text)
                except ValueError as exc:
                    raise ValueError("track point has invalid elevation") from exc
                break
        points.append((lat, lon, elevation))
    if len(points) < 2:
        raise ValueError("GPX must contain at least two track points")
    return points


def project(points: List[Point3], max_size_mm: float, elevation_scale: float) -> List[Point3]:
    """Project latitude/longitude locally, scale to millimetres, and center."""
    mean_lat = sum(p[0] for p in points) / len(points)
    lat_scale = 111_320.0
    lon_scale = lat_scale * math.cos(math.radians(mean_lat))
    raw = [(p[1] * lon_scale, p[0] * lat_scale, p[2]) for p in points]
    min_x, max_x = min(p[0] for p in raw), max(p[0] for p in raw)
    min_y, max_y = min(p[1] for p in raw), max(p[1] for p in raw)
    span = max(max_x - min_x, max_y - min_y)
    if span <= 0:
        raise ValueError("GPX track has no geographic extent")
    scale = max_size_mm / span
    center_x, center_y = (min_x + max_x) / 2, (min_y + max_y) / 2
    min_ele = min(p[2] for p in raw)
    return [
        ((x - center_x) * scale, (y - center_y) * scale, (z - min_ele) * elevation_scale)
        for x, y, z in raw
    ]
