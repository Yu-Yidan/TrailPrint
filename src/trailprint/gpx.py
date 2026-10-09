"""GPX parsing and geographic projection."""

import copy
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def gpx_segment_nodes(root):
    """Return point lists without connecting tracks, segments, or routes."""
    if _tag(root) != "gpx":
        raise ValueError("invalid GPX: expected a gpx document")
    segments = []
    for node in root.iter():
        kind = _tag(node)
        if kind in {"trkseg", "rte"}:
            point_kind = "trkpt" if kind == "trkseg" else "rtept"
            points = [child for child in node if _tag(child) == point_kind]
            if points:
                segments.append(points)
    if not segments:
        points = [node for node in root.iter() if _tag(node) in {"trkpt", "rtept"}]
        if points:
            segments.append(points)
    return segments


def _point(node):
    try:
        lat, lon = float(node.attrib["lat"]), float(node.attrib["lon"])
        if not (math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except (KeyError, ValueError) as exc:
        raise ValueError("track point has invalid latitude or longitude") from exc
    elevation = 0.0
    for child in node:
        if _tag(child) == "ele" and child.text:
            try:
                elevation = float(child.text)
                if not math.isfinite(elevation):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("track point has invalid elevation") from exc
    return lat, lon, elevation


def _validate_segments(root):
    segments = gpx_segment_nodes(root)
    if not segments or any(len(segment) < 2 for segment in segments):
        raise ValueError("GPX must contain at least two track points in each nonempty segment")
    return [[_point(node) for node in segment] for segment in segments]


def combine_gpx(sources):
    """Normalize GPX inputs to separate GPX 1.1 tracks, preserving point data."""
    namespace = "http://www.topografix.com/GPX/1/1"
    root = ET.Element(f"{{{namespace}}}gpx", version="1.1", creator="TrailPrint")
    for filename, data in sources:
        try:
            source = ET.fromstring(data)
            _validate_segments(source)
        except (ET.ParseError, ValueError) as exc:
            raise ValueError(f"{filename}: invalid GPX: {exc}") from exc
        source_namespace = source.tag.split("}")[0] + "}" if "}" in source.tag else ""
        track = ET.SubElement(root, f"{{{namespace}}}trk")
        ET.SubElement(track, f"{{{namespace}}}name").text = filename
        for points in gpx_segment_nodes(source):
            segment = ET.SubElement(track, f"{{{namespace}}}trkseg")
            for point in points:
                normalized = copy.deepcopy(point)
                for node in normalized.iter():
                    if (source_namespace and node.tag.startswith(source_namespace)) or "}" not in node.tag:
                        node.tag = f"{{{namespace}}}" + _tag(node)
                normalized.tag = f"{{{namespace}}}trkpt"
                segment.append(normalized)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _timestamp(node):
    for child in node:
        if _tag(child) == "time" and child.text:
            try:
                return datetime.fromisoformat(child.text.strip().replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def read_gpx_trip_stats(path: Path) -> TripStats:
    """Sum statistics within segments; gaps never contribute distance or time."""
    try:
        root = ET.parse(path).getroot()
        _validate_segments(root)
    except (OSError, ET.ParseError, ValueError):
        return TripStats()
    distance = elevation_gain = duration = 0.0
    duration_available = True
    timestamps = []
    for nodes in gpx_segment_nodes(root):
        points = [_point(node) for node in nodes]
        times = [_timestamp(node) for node in nodes]
        timestamps.extend(time for time in times if time is not None)
        for first, second in zip(points, points[1:]):
            lat1, lon1, ele1 = first
            lat2, lon2, ele2 = second
            phi1, phi2 = math.radians(lat1), math.radians(lat2)
            dphi = math.radians(lat2 - lat1)
            dlambda = math.radians(lon2 - lon1)
            value = (math.sin(dphi / 2) ** 2
                     + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
            value = min(1.0, max(0.0, value))
            distance += 6371.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))
            elevation_gain += max(0.0, ele2 - ele1)
        if times[0] is None or times[-1] is None:
            duration_available = False
        else:
            try:
                hours = (times[-1] - times[0]).total_seconds() / 3600
                if hours < 0:
                    duration_available = False
                else:
                    duration += hours
            except TypeError:
                duration_available = False
    earliest = min(timestamps, key=lambda time: time.replace(tzinfo=timezone.utc)
                   if time.tzinfo is None else time.astimezone(timezone.utc)) if timestamps else None
    return TripStats(distance, elevation_gain, duration if duration_available else 0.0,
                     earliest.strftime("%Y.%m.%d") if earliest else "")


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
        total_minutes = math.floor(stats.duration_hours * 60 + 1e-8)
        hours, minutes = divmod(total_minutes, 60)
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


def read_gpx_segments(path: Path) -> List[List[Point3]]:
    if not path.is_file():
        raise ValueError(f"missing GPX input: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"invalid GPX XML: {exc}") from exc
    return _validate_segments(root)


def read_gpx(path: Path) -> List[Point3]:
    """Flatten points for bounds/projection only; route geometry uses segments."""
    return [point for segment in read_gpx_segments(path) for point in segment]


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
