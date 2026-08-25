"""Fit OpenStreetMap land-cover evidence into low-waste elevation bands."""

import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path


MAX_TRANSITIONS = 3
TRANSITION_PENALTY = 0.08
TERRAIN_ROLES = ("black", "grey", "green", "white")
ATTRIBUTION = "© OpenStreetMap contributors"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json",
                                        dir=str(path.parent))
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def optimize_elevation_bands(classified, max_transitions=MAX_TRANSITIONS,
                             transition_penalty=TRANSITION_PENALTY) -> list:
    """Fit a piecewise-constant color sequence to elevation-sorted samples."""
    values = sorted((float(elevation), role) for elevation, role in classified
                    if role in TERRAIN_ROLES and math.isfinite(float(elevation)))
    if not values:
        return []
    bucket_count = min(64, max(1, int(math.sqrt(len(values)))))
    buckets = []
    for index in range(bucket_count):
        start = index * len(values) // bucket_count
        end = (index + 1) * len(values) // bucket_count
        chunk = values[start:end]
        if chunk:
            buckets.append({"min": chunk[0][0], "max": chunk[-1][0],
                            "counts": Counter(role for _, role in chunk)})
    n = len(buckets)
    segment = {}
    for start in range(n):
        counts = Counter()
        for end in range(start + 1, n + 1):
            counts.update(buckets[end - 1]["counts"])
            role, matched = min(
                ((role, counts[role]) for role in TERRAIN_ROLES),
                key=lambda item: (-item[1], TERRAIN_ROLES.index(item[0])))
            segment[start, end] = (sum(counts.values()) - matched, role)
    total = len(values)
    penalty = total * float(transition_penalty)
    states = {(0, 0): (0.0, [])}
    for band_count in range(1, max_transitions + 2):
        for end in range(1, n + 1):
            choices = []
            for start in range(end):
                prior = states.get((band_count - 1, start))
                if prior is None:
                    continue
                error, role = segment[start, end]
                choices.append((prior[0] + error + (penalty if band_count > 1 else 0),
                                prior[1] + [(start, end, role)]))
            if choices:
                states[band_count, end] = min(choices,
                                              key=lambda item: (item[0], item[1]))
    _, fitted = min((states[count, n] for count in range(1, max_transitions + 2)
                     if (count, n) in states), key=lambda item: (item[0], len(item[1])))
    merged = []
    for start, end, role in fitted:
        low, high = buckets[start]["min"], buckets[end - 1]["max"]
        if merged and merged[-1]["role"] == role:
            merged[-1]["max_elevation_m"] = high
        else:
            merged.append({"role": role, "min_elevation_m": low,
                           "max_elevation_m": high})
    for lower, upper in zip(merged, merged[1:]):
        boundary = (lower["max_elevation_m"] + upper["min_elevation_m"]) / 2
        lower["max_elevation_m"] = upper["min_elevation_m"] = boundary
    return merged


def print_bands(source_bands: list, elevation: dict, layer_height=0.2) -> list:
    minimum, maximum = elevation["source_min_m"], elevation["source_max_m"]
    relief_m = maximum - minimum
    relief_mm = elevation["model_surface_max_z_mm"] - elevation["model_surface_min_z_mm"]
    base, top = elevation["min_thickness_mm"], elevation["min_thickness_mm"] + relief_mm

    def print_z(value):
        return base + (min(maximum, max(minimum, value)) - minimum) / relief_m * relief_mm

    candidates = []
    for index, band in enumerate(source_bands):
        low = base if index == 0 else print_z(band["min_elevation_m"])
        high = top if index == len(source_bands) - 1 else print_z(band["max_elevation_m"])
        candidates.append({"role": band["role"], "min_z": low, "max_z": high,
                           "min_elevation_m": band["min_elevation_m"],
                           "max_elevation_m": band["max_elevation_m"]})
    bands = [band for band in candidates if band["max_z"] - band["min_z"] >= layer_height]
    if not bands:
        bands = [max(candidates, key=lambda band: band["max_z"] - band["min_z"])]
    bands[0]["min_z"], bands[0]["min_elevation_m"] = base, minimum
    bands[-1]["max_z"], bands[-1]["max_elevation_m"] = top, maximum
    for lower, upper in zip(bands, bands[1:]):
        boundary_z = (lower["max_z"] + upper["min_z"]) / 2
        boundary_m = (lower["max_elevation_m"] + upper["min_elevation_m"]) / 2
        lower["max_z"] = upper["min_z"] = boundary_z
        lower["max_elevation_m"] = upper["min_elevation_m"] = boundary_m
    return bands


def analyze_project(project: Path) -> dict:
    evidence = _read_json(project / "build/terrain-osm-evidence.json")
    elevation = _read_json(project / "build/elevation.json")
    if not evidence.get("available"):
        raise ValueError(evidence.get("error") or "OpenStreetMap terrain evidence is unavailable.")
    samples = evidence.get("samples", [])
    classified = [(float(item[0]), str(item[1])) for item in samples
                  if isinstance(item, list) and len(item) >= 3]
    if len(classified) < 20 or not elevation:
        raise ValueError("OpenStreetMap terrain evidence did not cover enough samples.")
    bands = print_bands(optimize_elevation_bands(classified), elevation)
    if not bands:
        raise ValueError("OSM-inspired terrain bands could not be calculated.")
    matched = 0
    for elevation_m, role in classified:
        fitted = next((band["role"] for band in bands
                       if band["min_elevation_m"] <= elevation_m
                       <= band["max_elevation_m"]), None)
        matched += fitted == role
    mapped = sum(bool(item[2]) for item in samples if isinstance(item, list) and len(item) >= 3)
    report = {
        "version": 2, "available": True, "source": "OpenStreetMap",
        "attribution": ATTRIBUTION, "sample_count": len(classified),
        "evidence_counts": evidence.get("evidence_counts", {}),
        "mapped_coverage": round(mapped / len(classified), 3),
        "confidence": round(matched / len(classified), 3), "bands": bands,
    }
    _atomic_json(project / "build/terrain-palette.json", report)
    return report


def load_palette(project: Path) -> dict:
    value = _read_json(project / "build/terrain-palette.json")
    return (value if value.get("version") == 2 and value.get("available")
            and value.get("source") == "OpenStreetMap"
            and isinstance(value.get("bands"), list) else {})
