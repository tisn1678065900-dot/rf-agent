---
name: rfdesign
description: Design an RF passive structure end to end - requirement text to an optimised, EM-verified layout committed into Altium. Use when someone asks for a power divider, splitter, coupler or matching structure at a stated frequency on a stated laminate, or asks to tune/verify an existing one in HFSS. Requires the rf-agent MCP server; pairs with eda-agent for the Altium side.
---

# RF design loop

You have two tool surfaces. `rf-agent` owns physics: line synthesis,
layout generation, HFSS, optimisation. `eda-agent` owns the board the
user has open in Altium. Keep them in their lanes -- do not try to place
RF copper with `pcb_place_tracks` by hand when `rf_export_altium` will do
it from a solved geometry.

## The loop

1. **`rf_doctor`** first, always. It tells you whether HFSS is reachable
   and whether the Altium bridge is live. Everything below assumes the
   answer was yes; if it was not, say so rather than producing numbers
   from the analytic model and calling them results.

2. **`rf_make_spec`** — turn the request into a spec. Get these from the
   user rather than inventing them:
   - centre frequency and bandwidth
   - laminate and thickness (`rf_doctor` lists what is modelled)
   - return loss, isolation, insertion loss, balance limits
   - whether the part sits in a metal housing

   If a number is missing, ask, or state the default you used in your
   reply. Silently assuming a 20% bandwidth is how a part gets built to
   the wrong requirement.

3. **`rf_synthesize`** — draw it without solving. Free. Check the board
   size, the DRC result and the PNG before spending solver time. If the
   seed already fails DRC, the spec and the fab rules disagree; fix that
   first, do not start a study.

4. **`rf_design`** — the closed loop. Returns a job id; poll
   `rf_job_status`. 40 trials on a 1:2 divider is roughly an hour of
   wall time. Tell the user that before you start it.

   Use `rf_optimize` instead when you only want the study and will handle
   verification yourself. Use `rf_solve` for a single candidate.

5. **Read the report** it writes. Then tell the user what the design
   *does*, not that the loop finished.

6. **`rf_export_altium`** — `dry_run=True` first. Look at the
   quantisation block: Altium's PCB commands take integer mils (25.4 um),
   and on a
   narrow high-impedance arm that rounding moves the impedance by a
   noticeable fraction of an ohm. The report's "as built" section is the
   honest number. Only then write for real.

## What to be careful about

**The analytic model is a seed, not an answer.** `rf_line_model` gets a
grounded-CPW width right to a fraction of a percent, and it still cannot
predict the drawn length of a Wilkinson arm, because the tee and the
bends add electrical length it does not see. On the reference design the
drawn arm ran ~19% over the ideal quarter wave. Never quote analytic
numbers as performance.

**Draft results are for ranking, not reporting.** The optimiser explores
at a coarse mesh. Only the verified full-fidelity solve goes in front of
a person. `rf_design` does this for you; if you drive `rf_optimize`
directly, run `rf_solve` on the winner with `draft=False` before you
report anything.

**The EM model is a cavity.** The outer boundary is PEC -- a metal
housing. If the part will sit in free space or under a plastic lid, say
that the model does not represent it.

**The pour must be stitched, not just fenced.** Ground vias along the
traces are not enough on thin high-Dk laminate; the pour interior
resonates as a patch. The generator floods the interior by default. If
someone asks you to thin the via count, that is what you are giving up.

**Isolation resistors are ideal in the model.** A real 0402's parasitic
inductance degrades isolation above roughly 10 GHz. An X-band isolation
figure from this loop is optimistic.

## Reading a result

`meets_spec` is a verdict on the *hard* targets only. A soft miss (phase
imbalance, by default) still passes. When reporting:

- lead with whether it meets the requirement, and by how much margin
- name the frequency where the worst case sits -- a notch at one
  frequency is a different problem from a whole band that is off
- quote the as-built numbers when they differ from the ideal ones
- say what the model did not include

## Handing off to Altium

`rf_export_altium` writes design intent: centrelines as tracks with
their real widths, ground vias as vias, and the coplanar ground as a
polygon pour plus a clearance rule sized to the EM model's gap. Altium
regenerates the copper from that, so the result is net-aware and
DRC-clean.

After it lands, `eda-agent` takes over: `pcb_run_drc` to confirm,
`design_lint_report` for the wider sweep, `pcb_repour_polygons` if the
pour needs refreshing. The DXF that `rf_design` writes is the
full-precision copy -- use it for the fab package and for merging into an
enclosure drawing, not the grid-quantised Altium copper.
