from types import SimpleNamespace

import pytest

from trailprint.blender_worker import configure_generation_layout, configure_plate_text


@pytest.mark.parametrize('shape,extra,layout', [
    ('HEXAGON', 'OUTER TEXT', 'OUTER_EDGE'),
    ('HEXAGON', 'FRONT TEXT', 'FRONT_FACE'),
    ('HEXAGON', 'INNER TEXT', 'ON_MAP'),
    ('CIRCLE', 'OUTER TEXT', 'CURVED'),
    ('OCTAGON', 'OUTER TEXT', 'OUTER_EDGE'),
    ('HEXAGON', 'NONE', 'NONE'),
])
def test_v4_shape_layout_is_explicit(shape, extra, layout):
    props = SimpleNamespace(textLayout='old')
    configure_generation_layout(props, dict(shape=shape, shape_extra=extra))
    assert props.textLayout == layout
    assert props.shapeExtrasActive == (extra != 'NONE')
    assert props.plateMode == 'SOLID_PLATE'
    assert not props.textPlacement
    assert props.elevationMode == 'PROPORTIONAL'


@pytest.mark.parametrize('shape,order', [
    ('HEXAGON', (5, 1, 2, 3, 4)),
    ('OCTAGON', (1, 2, 3, 4, 5)),
    ('CIRCLE', (1, 2, 3, 4, 5)),
])
def test_v4_app_fields_keep_their_edge_locations(shape, order):
    props = SimpleNamespace(textLayout='OUTER_EDGE')
    config = dict(shape=shape, plate_title='Title {name}',
                  **{f'plate_text_{i}': f'Edge {i} {{length}}' for i in range(1, 6)})
    configure_plate_text(props, config)
    assert props.titlefield == 'Title {name}'
    for index, source in enumerate(order, 1):
        assert getattr(props, f'textfield{index}') == config[f'plate_text_{source}']


def test_legacy_shape_layout_is_preserved():
    props = SimpleNamespace()
    configure_generation_layout(props, dict(shape='HEXAGON', shape_extra='OUTER TEXT'))
    assert props.shapeTextStyle == 'OUTER TEXT'
    assert not props.fixedElevationScale


def test_v4_water_switches_are_enabled_and_can_be_disabled():
    from trailprint.blender_worker import configure_water

    props = SimpleNamespace(show_water=False)
    configure_water(props, True)
    assert props.show_water and props.elementChoice and props.col_wBodiesActive and props.col_wMajorActive
    assert not props.col_wMinorActive
    assert props.el_oActive and props.elementSource == 'OSM'
    configure_water(props, False)
    assert not props.show_water and not props.col_wBodiesActive and not props.col_wMajorActive
    assert not props.col_wMinorActive and not props.el_oActive


def test_major_water_query_retains_wikidata_streams_without_all_minor_waterways():
    from trailprint.blender_worker import major_water_query_compat
    query = '[out:json][bbox:1,2,3,4];(way["waterway"~"river|canal"];);out body;'
    result = major_water_query_compat(query)
    assert 'way["waterway"~"stream|ditch|drain"]["wikidata"];' in result
    assert 'way["waterway"~"stream|ditch|drain"];' not in result
    assert major_water_query_compat(None) is None
    assert major_water_query_compat('legacy query') == 'legacy query'
