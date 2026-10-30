from shapely.affinity import scale
from shapely.geometry import LineString, Point, box

from trailprint.shoreline import smooth_shoreline


def pier(width):
    outline = box(-10, -10, 10, 10)
    land = box(-10, -10, 10, 0).union(box(-width / 2, 0, width / 2, 3))
    return outline, land


def test_small_pier_is_blended_into_shoreline():
    outline, land = pier(.6)
    water, report = smooth_shoreline(outline.difference(land), outline)
    assert water.covers(Point(0, 2))
    assert not water.covers(Point(0, -1))
    assert report["smoothed_patch_count"] > 0
    assert report["removed_land_area_mm2"] > 1
    assert report["water_components_before"] == report["water_components_after"]


def test_wider_pier_is_retained_with_rounded_tip():
    outline, land = pier(1.6)
    water, _ = smooth_shoreline(outline.difference(land), outline)
    assert not water.covers(Point(0, 2.7))
    assert water.covers(Point(.75, 2.95))


def test_decisions_follow_physical_model_scale():
    outline, land = pier(.6)
    small, _ = smooth_shoreline(outline.difference(land), outline)
    large_outline = scale(outline, 2, 2, origin=(0, 0))
    large_land = scale(land, 2, 2, origin=(0, 0))
    large, _ = smooth_shoreline(large_outline.difference(large_land), large_outline)
    assert small.covers(Point(0, 2))
    assert not large.covers(Point(0, 4))


def test_small_island_is_preserved_when_it_cannot_be_smoothed():
    outline = box(-10, -10, 10, 10)
    island = Point(0, 0).buffer(.2)
    water, report = smooth_shoreline(outline.difference(island), outline)
    assert water.intersection(island).area < 1e-8
    assert report["protected_edit_count"] == 1


def test_large_land_connection_is_not_severed():
    outline = box(-10, -10, 10, 10)
    land = box(-10, -10, 10, 0).union(box(-.3, 0, .3, 4)).union(box(-2, 4, 2, 7))
    water, report = smooth_shoreline(outline.difference(land), outline)
    remaining = outline.difference(water)
    assert remaining.geom_type == "Polygon"
    assert remaining.covers(Point(0, 2))
    assert remaining.covers(Point(0, 5))
    assert report["protected_edit_count"] > 0


def test_real_barrier_between_water_bodies_is_preserved():
    outline = box(-10, -10, 10, 10)
    original = box(-8, -8, -.06, 8).union(box(.06, -8, 8, 8))
    water, report = smooth_shoreline(original, outline)
    assert not water.covers(Point(0, 0))
    assert water.geom_type == "MultiPolygon"
    assert report["water_components_after"] == 2


def test_other_water_level_cannot_be_joined():
    outline = box(-10, -10, 10, 10)
    lake = box(-8, -8, -.06, 8)
    sea = box(.06, -8, 8, 8)
    water, _ = smooth_shoreline(lake, outline, other_water=sea)
    assert water.distance(sea) >= .119
    assert water.intersection(sea).area == 0


def test_trail_support_and_model_boundary_are_protected():
    outline, land = pier(.6)
    original = outline.difference(land)
    trail = LineString([(0, -1), (0, 3)]).buffer(.1)
    water, report = smooth_shoreline(original, outline, protected=trail)
    assert not water.covers(Point(0, 2))
    rim = outline.difference(outline.buffer(-.49))
    assert water.intersection(rim).symmetric_difference(original.intersection(rim)).area < 1e-8
    assert report["protected_area_mm2"] > 0


def test_thin_dead_end_water_inlet_is_smoothed_into_land():
    outline = box(-10, -10, 10, 10)
    original = box(-10, -10, 10, 0).union(box(-.3, 0, .3, 5))
    water, report = smooth_shoreline(original, outline, smooth_water=True)
    assert not water.covers(Point(0, 3))
    assert water.covers(Point(0, -2))
    assert report["removed_water_area_mm2"] > 2
    assert report["smoothed_water_patch_count"] > 0


def test_larger_water_branch_and_protected_channel_are_retained():
    outline = box(-10, -10, 10, 10)
    original = box(-10, -10, 10, 0).union(box(-.8, 0, .8, 5))
    water, _ = smooth_shoreline(original, outline, smooth_water=True)
    assert water.covers(Point(0, 4))
    thin = box(-10, -10, 10, 0).union(box(-.3, 0, .3, 5))
    route = LineString([(0, -1), (0, 5)]).buffer(.1)
    water, _ = smooth_shoreline(thin, outline, protected=route, smooth_water=True)
    assert water.covers(Point(0, 3))


def test_narrow_water_connection_and_island_remain_separate():
    outline = box(-10, -10, 10, 10)
    # A channel connects two large water areas; smoothing cannot sever it.
    original = box(-8, -8, 8, -2).union(box(-.3, -2, .3, 2)).union(box(-8, 2, 8, 8))
    water, _ = smooth_shoreline(original, outline, smooth_water=True)
    assert water.geom_type == "Polygon"
    assert water.covers(Point(0, 0))
