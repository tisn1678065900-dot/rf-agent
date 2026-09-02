import ezdxf
from shapely.geometry import Point
import pytest

from rf_agent.devices import get_device
from rf_agent.export import quantisation_report, snap_geometry, write_dxf
from rf_agent.export.altium import MM_PER_MIL, AltiumWriter, mm_to_mil
from rf_agent.spec import RFSpec


@pytest.fixture
def design():
    spec = RFSpec.divider(f0_ghz=11.6, n_way=2)
    dev = get_device(spec.device)
    return spec, dev.build(spec, dev.seed_params(spec))


def test_dxf_carries_every_layer(tmp_path, design):
    spec, g = design
    p = write_dxf(g, tmp_path / "d.dxf")
    doc = ezdxf.readfile(p)
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert {"RF_TOP_SIGNAL", "RF_TOP_POUR", "RF_BOARD_OUTLINE", "RF_VIA"} <= layers
    assert doc.header["$INSUNITS"] == 4  # mm


def test_dxf_has_a_circle_pair_per_via(tmp_path, design):
    spec, g = design
    doc = ezdxf.readfile(write_dxf(g, tmp_path / "d.dxf"))
    circles = [e for e in doc.modelspace() if e.dxftype() == "CIRCLE"]
    assert len(circles) == 2 * len(g.vias)


def test_quantisation_reports_the_impedance_the_grid_gives(design):
    spec, g = design
    q = quantisation_report(g, spec)
    assert q["grid_um"] == pytest.approx(25.4)
    assert q["widths"]
    for row in q["widths"]:
        # the snapped width really is on the mil grid
        assert row["snapped_mm"] == pytest.approx(row["width_mil"] * MM_PER_MIL, abs=1e-9)
        # and the reported shift is the difference the model actually gives
        assert abs(row["error_um"]) <= 12.71
    assert q["worst_z0_shift_ohm"] >= 0


def test_snap_geometry_puts_every_width_on_the_grid(design):
    spec, g = design
    s = snap_geometry(g)
    for path in s.paths:
        for pr in path.prims:
            assert pr.width / MM_PER_MIL == pytest.approx(
                round(pr.width / MM_PER_MIL), abs=1e-9
            )
    # and it is still the same structure
    assert len(s.paths) == len(g.paths)
    assert len(s.vias) == len(g.vias)
    assert s.trace.area == pytest.approx(g.trace.area, rel=0.10)


def test_write_plan_is_resolvable_without_altium(design):
    spec, g = design
    plan = AltiumWriter(origin_mils=(2000, 2000)).plan(g, spec)
    assert plan.outline is not None
    assert plan.tracks and plan.vias
    assert "GND" in plan.nets
    # every track has a positive integer width and non-zero length
    for t in plan.tracks:
        assert isinstance(t["width"], int) and t["width"] >= 1
        assert (t["x1"], t["y1"]) != (t["x2"], t["y2"])
    # the pour is a polygon on the signal layer carrying the ground net
    assert plan.polygon["net"] == "GND"
    assert plan.polygon["layer"] == "TopLayer"
    # and the clearance rule matches the EM model's coplanar gap
    assert plan.rules[0]["value"] == mm_to_mil(spec.stackup.gap_mm)


def test_dry_run_never_touches_altium(design, monkeypatch):
    spec, g = design
    w = AltiumWriter()

    def boom(*a, **k):
        raise AssertionError("dry_run must not reach the bridge")

    monkeypatch.setattr(AltiumWriter, "preflight", boom)
    out = w.write(g, spec, dry_run=True)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert "quantisation" in out
    assert out["plan"]["n_tracks"] > 0


def test_tracks_land_inside_the_declared_outline(design):
    spec, g = design
    plan = AltiumWriter(origin_mils=(2000, 2000)).plan(g, spec)
    x1, y1, x2, y2 = plan.outline
    lo_x, hi_x = min(x1, x2), max(x1, x2)
    lo_y, hi_y = min(y1, y2), max(y1, y2)
    for t in plan.tracks:
        for x, y in ((t["x1"], t["y1"]), (t["x2"], t["y2"])):
            assert lo_x - 1 <= x <= hi_x + 1
            assert lo_y - 1 <= y <= hi_y + 1


def test_snap_geometry_keeps_copper_that_is_not_a_routed_path(design):
    """Resistor lands are signal copper but belong to no Path.

    Rebuilding the trace from the paths alone drops them, which leaves the
    lumped isolation resistor bridging bare substrate. That does not fail
    loudly -- it comes back as an 11 dB isolation collapse that reads like
    physics.
    """
    spec, g = design
    from shapely.ops import unary_union

    routed = unary_union([p.polygon() for p in g.paths])
    extras = g.trace.difference(routed)
    assert extras.area > 0, "this device should have non-path copper to protect"

    s = snap_geometry(g)
    for r in g.resistors:
        for sign in (+1, -1):
            land = (r.x, r.y + sign * (r.gap / 2 + r.land / 4))
            assert s.trace.covers(Point(land)), f"land at {land} lost by snapping"
    # and the extra copper survived essentially intact
    assert s.trace.difference(unary_union([p.polygon() for p in s.paths])).area == (
        pytest.approx(extras.area, rel=0.05)
    )


def test_connected_copper_gets_one_net_not_one_per_run(design):
    """A Wilkinson is one continuous piece of signal copper.

    The generator names its runs separately (feed, arms, outputs) for its
    own bookkeeping. Writing those as separate Altium nets would report a
    short at every tee, so connectivity has to decide the netlist.
    """
    spec, g = design
    assert len({p.net for p in g.paths}) > 2, "generator should use several run names"

    plan = AltiumWriter().plan(g, spec)
    signal_nets = [n for n in plan.nets if n != "GND"]
    assert len(signal_nets) == 1, f"expected one signal net, got {signal_nets}"
    # every routed track carries it
    assert {t["net_name"] for t in plan.tracks} == set(signal_nets)


def test_electrically_separate_runs_keep_separate_nets(design):
    """The grouping must not simply merge everything it is handed."""
    spec, g = design
    from rf_agent.geometry import Geometry, route

    far = route([(100.0, 100.0), (105.0, 100.0)], width=0.5, radius=0.0, net="OTHER")
    detached = Geometry(
        paths=list(g.paths) + [far],
        trace=g.trace, pour=g.pour, vias=g.vias, resistors=g.resistors,
        ports=g.ports, board=g.board,
    )
    groups = AltiumWriter._net_groups(detached)
    assert groups[len(g.paths)] != groups[0]
    assert len(set(groups.values())) == 2
