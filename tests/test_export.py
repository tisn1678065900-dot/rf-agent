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
    # the coplanar pour on the signal layer, and the ground plane the
    # vias drop to -- both on the ground net
    layers = {g["layer"]: g for g in plan.polygons}
    assert set(layers) == {"TopLayer", "BottomLayer"}
    assert all(g["net"] == "GND" for g in plan.polygons)
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


def test_nets_are_pipe_separated_for_the_bridge(design, monkeypatch):
    """The bridge splits the net list on "|".

    Joined with a comma instead, the whole list arrives as one net named
    "RF_IN,GND". Nothing errors: the nets command succeeds, and then every
    track, via and polygon that asks for "RF_IN" or "GND" fails its lookup
    silently and lands with no net. The board looks right in Altium and is
    electrically inert -- the pour does not connect to its own stitching
    vias. Caught on real hardware, so it is pinned here.
    """
    spec, g = design
    w = AltiumWriter()
    sent: list[tuple[str, dict]] = []

    class Bridge:
        def send_command(self, cmd, params, timeout=None):
            sent.append((cmd, params))
            return {"ok": True}

    monkeypatch.setattr(AltiumWriter, "preflight", lambda self: {"ok": True})
    monkeypatch.setattr(type(w), "bridge", property(lambda self: Bridge()))
    w.write(g, spec)

    nets = next(p for c, p in sent if c == "pcb.create_nets_from_list")["nets"]
    assert "," not in nets, f"net list must not be comma-joined, got {nets!r}"
    assert set(nets.split("|")) == {"RF_IN", "GND"}


def test_every_track_and_via_carries_a_net(design):
    """Copper with no net is copper Altium's connectivity engine ignores."""
    spec, g = design
    plan = AltiumWriter().plan(g, spec)
    assert plan.nets, "the plan must declare its nets"
    assert all(t["net_name"] for t in plan.tracks), "a track has no net"
    assert all(v["net"] for v in plan.vias), "a via has no net"
    assert plan.polygons, "no pour at all"
    assert all(g["net"] for g in plan.polygons), "a pour has no net"
    # Whatever the tracks and vias claim must be a net the plan creates.
    used = {t["net_name"] for t in plan.tracks} | {v["net"] for v in plan.vias}
    used |= {g["net"] for g in plan.polygons}
    assert used <= set(plan.nets), f"{used - set(plan.nets)} never created"


def test_a_ground_plane_is_placed_under_the_board(design):
    """The EM model sits on a solid conductor at z=0; the board must too.

    Without a bottom-layer plane every stitching via lands on bare
    laminate, and the coplanar pour stays in the electrically separate
    islands the traces cut it into -- on this divider, three of them.
    Altium reported five unrouted GND connections on the first real
    write for exactly this reason.
    """
    spec, g = design
    plan = AltiumWriter().plan(g, spec)
    ground = [p for p in plan.polygons if p["layer"] == "BottomLayer"]
    assert len(ground) == 1, "exactly one ground plane expected"
    assert ground[0]["net"] == "GND"
    # It has to span at least the whole routed area, or the vias at the
    # edges still land on nothing.
    x1, y1, x2, y2 = plan.outline
    assert ground[0]["x1"] <= x1 and ground[0]["y1"] <= y1
    assert ground[0]["x2"] >= x2 and ground[0]["y2"] >= y2
    # Every via must reach it.
    assert all(v["net"] == "GND" for v in plan.vias)
