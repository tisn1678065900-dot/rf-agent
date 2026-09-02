import math

import pytest
from shapely.geometry import box

from rf_agent.geometry import (
    Arc,
    Seg,
    check_drc,
    fence_vias,
    fill_vias,
    make_pour,
    route,
)
from rf_agent.spec import RFSpec


def test_route_without_corners_is_one_segment():
    p = route([(0, 0), (5, 0)], width=0.5, radius=1.0)
    assert len(p.prims) == 1
    assert isinstance(p.prims[0], Seg)
    assert p.length == pytest.approx(5.0)


def test_route_fillets_a_right_angle_into_seg_arc_seg():
    p = route([(0, 0), (5, 0), (5, 5)], width=0.5, radius=1.0)
    kinds = [type(x).__name__ for x in p.prims]
    assert kinds == ["Seg", "Arc", "Seg"]
    arc = p.prims[1]
    assert arc.radius == pytest.approx(1.0)
    assert abs(arc.sweep) == pytest.approx(math.pi / 2)
    # a filleted corner is shorter than the mitred one
    assert p.length < 10.0


def test_fillet_shrinks_rather_than_overrunning_a_short_leg():
    # 0.3 mm leg cannot hold a 1 mm radius; the arc must shrink, not
    # spill past the corner.
    p = route([(0, 0), (0.3, 0), (0.3, 5)], width=0.2, radius=1.0)
    arc = next(x for x in p.prims if isinstance(x, Arc))
    assert arc.radius < 1.0
    xs = [c[0] for pr in p.prims for c in pr.centreline()]
    assert max(xs) <= 0.3 + 1e-9


def test_path_polygon_area_tracks_width():
    thin = route([(0, 0), (10, 0)], width=0.2, radius=0).polygon().area
    thick = route([(0, 0), (10, 0)], width=0.4, radius=0).polygon().area
    assert thick == pytest.approx(2 * thin, rel=1e-3)


def test_pour_keeps_the_requested_gap():
    board = box(-5, -5, 5, 5)
    trace = route([(-5, 0), (5, 0)], width=0.5, radius=0).polygon()
    pour = make_pour(board, trace, gap=0.25)
    assert pour.distance(trace) == pytest.approx(0.25, abs=1e-6)


def test_pour_drops_islands_too_small_to_stitch():
    board = box(-5, -5, 5, 5)
    # two traces close enough that the strip between them cannot hold a via
    a = route([(-5, 0.4), (5, 0.4)], width=0.5, radius=0).polygon()
    b = route([(-5, -0.4), (5, -0.4)], width=0.5, radius=0).polygon()
    from shapely.ops import unary_union

    trace = unary_union([a, b])
    pour = make_pour(board, trace, gap=0.25, min_via_clear=0.35)
    # nothing survives between them
    assert not pour.intersects(box(-4, -0.1, 4, 0.1))


def test_fence_offset_follows_each_trace_width():
    board = box(-6, -6, 6, 6)
    wide = route([(-5, 3), (5, 3)], width=1.0, radius=0)
    narrow = route([(-5, -3), (5, -3)], width=0.2, radius=0)
    from shapely.ops import unary_union

    trace = unary_union([wide.polygon(), narrow.polygon()])
    pour = make_pour(board, trace, gap=0.25)
    vias = fence_vias(
        [wide, narrow], pour, pitch=1.2, drill=0.3, pad=0.6, edge_clear=0.9
    )
    # each row sits 0.9 mm off its own trace edge, so the wide line's
    # fence is further from its centreline than the narrow one's
    near_wide = min(abs(abs(v.y - 3) - 1.4) for v in vias if v.y > 0)
    near_narrow = min(abs(abs(v.y + 3) - 1.0) for v in vias if v.y < 0)
    assert near_wide < 0.05
    assert near_narrow < 0.05


def test_fill_vias_stay_inside_the_pour():
    board = box(-5, -5, 5, 5)
    trace = route([(-5, 0), (5, 0)], width=0.5, radius=0).polygon()
    pour = make_pour(board, trace, gap=0.25)
    vias = fill_vias(pour, pitch=2.5, drill=0.3, pad=0.6)
    assert vias
    from shapely.geometry import Point

    assert all(pour.contains(Point(v.x, v.y)) for v in vias)


def test_drc_flags_a_trace_below_the_fab_minimum():
    spec = RFSpec.divider(f0_ghz=11.6)
    from rf_agent.devices import get_device

    dev = get_device(spec.device)
    p = dev.seed_params(spec)
    g = dev.build(spec, p)
    assert check_drc(g, spec.stackup.fab) == []

    spec.stackup.fab.min_trace_mm = 1.0  # nothing on this board is that wide
    assert any("min_trace" in v for v in check_drc(g, spec.stackup.fab))
