"""Ordered, source-elevation terrain colors shared by preview and export."""

import math

COLORS = {
    "charcoal": ("PLA Matte Charcoal Black", "#000000"),
    "brown": ("PLA Matte Dark Brown", "#7D6556"),
    "ivory": ("PLA Matte Ivory White", "#FFFFFF"),
    "nardo": ("PLA Matte Nardo Grey", "#757575"),
    "green": ("PLA Matte Dark Green", "#68724D"),
}


def validate_ranges(ranges, elevation, layer_height=0.2):
    if not isinstance(ranges, (list, tuple)) or not 1 <= len(ranges) <= 5:
        raise ValueError("Choose between one and five terrain bands.")
    if not elevation:
        raise ValueError("Rebuild the terrain before configuring elevation bands.")
    minimum, maximum = elevation["source_min_m"], elevation["source_max_m"]
    relief = elevation["model_surface_max_z_mm"] - elevation["model_surface_min_z_mm"]
    result = []
    low = minimum
    for item in ranges:
        high = float(item["upper_m"])
        if (
            item["color"] not in COLORS
            or not math.isfinite(high)
            or high <= low
            or high > maximum + 1e-6
        ):
            raise ValueError("Terrain bands must have ordered elevations and valid colors.")
        if len(ranges) > 1 and (high - low) / (maximum - minimum) * relief < layer_height - 1e-6:
            raise ValueError("Each terrain band must be at least one nominal layer thick.")
        result.append(dict(upper_m=high, color=item["color"]))
        low = high
    if abs(low - maximum) > 1e-6:
        raise ValueError("Terrain bands must cover the full elevation range.")
    return result


def export_ranges(ranges, elevation, layer_height=0.2):
    ranges = validate_ranges(ranges, elevation, layer_height)
    low = elevation["source_min_m"]
    span = elevation["source_max_m"] - low
    relief = elevation["model_surface_max_z_mm"] - elevation["model_surface_min_z_mm"]
    base = elevation["min_thickness_mm"]
    result = []
    for index, item in enumerate(ranges):
        high = item["upper_m"]
        result.append(
            dict(
                role="band_" + str(index),
                min_z=base + (low - elevation["source_min_m"]) / span * relief,
                max_z=base + (high - elevation["source_min_m"]) / span * relief,
                min_elevation_m=low,
                max_elevation_m=high,
                color=item["color"],
            )
        )
        low = high
    return result
