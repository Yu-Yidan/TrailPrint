"""GPX parsing and geographic projection."""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

Point3 = Tuple[float, float, float]
AUTO_RELIEF_LIMIT_MM = 15.0


@dataclass(frozen=True)
class TripStats:
    distance_km: float = 0.0
    elevation_gain_m: float = 0.0
    duration_hours: float = 0.0
    date: str = ""

    @property
    def average_speed_kmh(self) -> float:
        return self.distance_km / self.duration_hours if self.duration_hours > 0 else 0.0


def read_gpx_trip_stats(path: Path) -> TripStats:
    """Calculate the same basic route statistics used by TrailPrint3D."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return TripStats()
    points = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in {"trkpt", "rtept"}:
            continue
        try:
            lat, lon = float(node.attrib["lat"]), float(node.attrib["lon"])
        except (KeyError, ValueError):
            continue
        elevation = 0.0
        timestamp = None
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "ele" and child.text:
                try:
                    elevation = float(child.text)
                except ValueError:
                    pass
            elif name == "time" and child.text:
                try:
                    timestamp = datetime.fromisoformat(child.text.strip().replace("Z", "+00:00"))
                except ValueError:
                    pass
        points.append((lat, lon, elevation, timestamp))
    if not points:
        return TripStats()
    distance = 0.0
    elevation_gain = 0.0
    earth_radius_km = 6371.0
    for first, second in zip(points, points[1:]):
        lat1, lon1, ele1, _ = first
        lat2, lon2, ele2, _ = second
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        value = (math.sin(dphi / 2) ** 2
                 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
        value = min(1.0, max(0.0, value))
        distance += earth_radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))
        elevation_gain += max(0.0, ele2 - ele1)
    first_time, last_time = points[0][3], points[-1][3]
    duration = 0.0
    if first_time is not None and last_time is not None:
        try:
            duration = max(0.0, (last_time - first_time).total_seconds() / 3600)
        except TypeError:
            duration = 0.0
    first_available_time = next((point[3] for point in points if point[3] is not None), None)
    date = first_available_time.strftime("%Y.%m.%d") if first_available_time else ""
    return TripStats(distance, elevation_gain, duration, date)


def format_trip_stats(stats: TripStats, unit_system: str = "IMPERIAL") -> dict:
    metric = str(unit_system).upper() == "METRIC"
    distance = (f"{stats.distance_km:.2f} km" if metric
                else f"{stats.distance_km * 0.621371192:.2f} mi")
    elevation = (f"{stats.elevation_gain_m:.0f} m" if metric
                 else f"{stats.elevation_gain_m * 3.280839895:.0f} ft")
    speed = (f"{stats.average_speed_kmh:.2f} km/h" if metric
             else f"{stats.average_speed_kmh * 0.621371192:.2f} mph")
    duration = ""
    if stats.duration_hours > 0:
        hours = int(stats.duration_hours)
        minutes = int((stats.duration_hours - hours) * 60)
        duration = f"{hours}h {minutes}m"
    return {"length": distance, "elevation": elevation, "duration": duration,
            "date": stats.date, "speed": speed}


def resolve_trip_text(value: str, stats: TripStats, unit_system: str, **values) -> str:
    replacements = format_trip_stats(stats, unit_system)
    replacements.update({key: str(replacement or "") for key, replacement in values.items()})
    result = str(value)
    for name, replacement in replacements.items():
        result = result.replace("{" + name + "}", replacement)
    return result


def read_gpx_trip_date(path: Path) -> str:
    """Return the first valid GPX point timestamp as YYYY.MM.DD."""
    return read_gpx_trip_stats(path).date


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


def estimate_print_relief_mm(path: Path, map_size_mm: float = 100.0):
    """Estimate TrailPrint3D relief from embedded GPX elevations and bounds."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    points = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in {"trkpt", "rtept"}:
            continue
        try:
            lat, lon = float(node.attrib["lat"]), float(node.attrib["lon"])
        except (KeyError, ValueError):
            continue
        elevation = None
        for child in node:
            if child.tag.rsplit("}", 1)[-1] == "ele" and child.text:
                try:
                    elevation = float(child.text)
                except ValueError:
                    pass
                break
        points.append((lat, lon, elevation))
    elevated = [point for point in points if point[2] is not None]
    if len(points) < 2 or len(elevated) < 2:
        return None
    earth_radius_km = 6371.0

    def mercator(lat, lon):
        bounded_lat = min(89.0, max(-89.0, lat))
        return (earth_radius_km * math.radians(lon),
                earth_radius_km * math.log(
                    math.tan(math.pi / 4 + math.radians(bounded_lat) / 2)))

    projected = [mercator(point[0], point[1]) for point in points]
    width = max(point[0] for point in projected) - min(point[0] for point in projected)
    height = max(point[1] for point in projected) - min(point[1] for point in projected)
    diagonal = math.hypot(width, height)
    if diagonal <= 0:
        return None
    horizontal_scale = float(map_size_mm) / diagonal
    heights = [(elevation / 1000.0) * horizontal_scale / math.cos(math.radians(lat))
               for lat, _lon, elevation in elevated]
    relief = max(heights) - min(heights)
    return relief if math.isfinite(relief) and relief >= 0 else None


def gpx_center_latitude(path: Path) -> float:
    try:
        points = read_gpx(path)
    except ValueError:
        return 0.0
    return (min(point[0] for point in points) + max(point[0] for point in points)) / 2


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
