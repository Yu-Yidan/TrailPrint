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
