"""Smooth shoreline details while preserving important land and water connections."""

from shapely import get_parts
from shapely.geometry import Polygon
from shapely.ops import unary_union

SHORELINE_DETAIL_MM = 1.0
MIN_EDIT_AREA_MM2 = .0001


def _parts(geometry):
    return [p for p in get_parts(geometry) if p.geom_type == "Polygon" and not p.is_empty]


def smooth_shoreline(water, outline, protected=None, other_water=None, smooth_water=False):
    """Smooth small land tips and, optionally, narrow dead-end water inlets.

    Water contraction requires the caller to restore original terrain beneath
    the removed inlet. Each edit preserves land and water connectivity.
    """
    radius = SHORELINE_DETAIL_MM / 2
    original = water.intersection(outline).buffer(0)
    other = (other_water if other_water is not None else Polygon()).intersection(outline)
    protected = protected if protected is not None else Polygon()
    frozen = unary_union([outline.difference(outline.buffer(-radius)),
                          protected.buffer(radius)])
    all_water = original.union(other).buffer(0)
    land = outline.difference(all_water).buffer(0)
    water_count = len(_parts(all_water))
    result = original
    accepted = rejected = 0
    protected_area = 0.0
    water_edits = 0
    for component in _parts(land):
        opened = component.buffer(-radius).buffer(radius).intersection(component).buffer(0)
        if opened.is_empty:
            # A narrow island or land barrier is retained as a protected feature.
            rejected += 1
            protected_area += component.area
            continue
        changes = component.difference(opened).buffer(0)
        protected_area += changes.intersection(frozen).area
        remaining = component
        for patch in _parts(changes.difference(frozen)):
            if patch.area < MIN_EDIT_AREA_MM2 or patch.distance(original) > 1e-7:
                continue
            next_land = remaining.difference(patch).buffer(0)
            next_water = all_water.union(patch).buffer(0)
            if ((not other.is_empty and patch.distance(other) < 1e-7)
                    or len(_parts(next_land)) != 1
                    or len(_parts(next_water)) != water_count):
                rejected += 1
                protected_area += patch.area
                continue
            result = result.union(patch).buffer(0)
            all_water = next_water
            remaining = next_land
            accepted += 1
    if smooth_water:
        land_count = len(_parts(outline.difference(all_water)))
        for component in _parts(result):
            opened = component.buffer(-radius).buffer(radius).intersection(component).buffer(0)
            if opened.is_empty:
                rejected += 1
                protected_area += component.area
                continue
            changes = component.difference(opened).buffer(0)
            protected_area += changes.intersection(frozen).area
            remaining = component
            for patch in _parts(changes.difference(frozen)):
                if patch.area < MIN_EDIT_AREA_MM2:
                    continue
                next_component = remaining.difference(patch).buffer(0)
                next_all = all_water.difference(patch).buffer(0)
                next_land = outline.difference(next_all).buffer(0)
                if (len(_parts(next_component)) != 1
                        or len(_parts(next_all)) != water_count
                        or len(_parts(next_land)) != land_count):
                    rejected += 1
                    protected_area += patch.area
                    continue
                result = result.difference(patch).buffer(0)
                all_water = next_all
                remaining = next_component
                water_edits += 1
    return result, {
        "detail_scale_mm": SHORELINE_DETAIL_MM,
        "radius_mm": radius,
        "smoothed_patch_count": accepted,
        "smoothed_water_patch_count": water_edits,
        "protected_edit_count": rejected,
        "protected_area_mm2": float(protected_area),
        "removed_land_area_mm2": float(result.difference(original).area),
        "removed_water_area_mm2": float(original.difference(result).area),
        "water_components_before": len(_parts(original)),
        "water_components_after": len(_parts(result)),
    }
