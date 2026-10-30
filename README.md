# TrailPrint

TrailPrint uses EmGi's TrailPrint3D Blender add-on to turn a GPX track into a
printable terrain map for Bambu Studio. Each design lives in
`prints/<four-digit-id>-<slug>/`
with its source, scratch build data, and final deliverables.

The local Analytics page summarizes project IDs by workflow status and plots
daily project creation in Pacific time without an external chart service.

## Quick start

Python 3.9+, Blender 5.2, and an enabled TrailPrint3D add-on are required.

Generated titles and model text use Arial Bold from the installed macOS font
file. Builds fail with a clear error if it is missing. Title-fit warnings use
the same font metrics. Existing exports keep their current lettering; rebuilding
a project uses Arial Bold. The font change preserves the existing text-size
normalization and does not change print settings.

Each build prepares a private font copy with a thicker comma tail, matching the
approved print coupon. Comma heads, lengths, spacing, and all other characters
retain their original design. The installed Arial Bold font is never modified.

TrailPrint remembers its own project filament palette. Opening a single-color
project in Bambu Studio does not remove these choices. To update the palette,
sync the AMS in Bambu Studio, then click **Import filaments from Bambu Studio**
on the model preview page. Saved choices follow matching profiles and colors;
removed colors require a replacement selection. These project filaments are
independent of physical AMS slots. Background exports use isolated Bambu data
and the palette captured when submitted, without changing Studio preferences.

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/trailprint build prints/0001-example-trail
```

The command launches Blender in the background and writes TrailPrint3D's STL
files to `prints/0001-example-trail/deliverables/`. Import them into Bambu Studio,
choose the printer and slicing settings, then save the 3MF project there.

See [docs/workflow.md](docs/workflow.md) for the complete workflow.

## Local interface

Start the project dashboard and open <http://127.0.0.1:5000>:

```sh
.venv/bin/trailprint web
```

The interface lists existing projects, accepts GPX files by drag-and-drop, and
can run TrailPrint3D from a project detail page. The reduced interface exposes
shape, shape extras, plate text, terrain offsets, and resolution while always
enabling water and single-color trail mode. Ponds, major rivers, and oceans
remain enabled; minor waterways are excluded. The complete downloaded DEM is
measured before displacement; relief above 15 mm is scaled down exactly while
flatter terrain retains its natural scale. With the standard 2 mm base, terrain
is therefore at most about 17 mm thick. Projects can be
sorted by their zero-padded four-digit filesystem ID; analytics retain the ID
as an integer, and the prefix is omitted from headings and printable titles.
Projects can be
deleted from the dashboard after typing the exact folder
slug; deleted folders remain recoverable in `.trailprint-trash/` for 30 days.
Use the **Deleted projects** link on the dashboard to restore them before they
expire. That page also supports selecting one, several, or all deleted projects
for permanent deletion after typing `DELETE`; permanent deletion cannot be
undone. The bundled `example-trail` project is protected. Run the command from the
repository root so it can find `prints/`.

Select or drop multiple GPX files to put swim, bike, and run recordings on one
fridge magnet. Uploads may total up to 10 MB. Original files are retained under
`inputs/gpx/`, with their names recorded in `gpx_sources.json`; `route.gpx` holds
the combined tracks without connecting separate segments. All routes use the
existing trail colour and share one terrain footprint. A long route may make
shorter routes appear small. Distance and elevation gain are summed within
segments, and duration adds recorded segment times without gaps between them.
If any segment lacks usable start/end timestamps, automatic duration is blank.
GPX timing metadata is retained independently of manually entered plate text.

Water bodies with non-manifold shoreline point contacts receive a single
0.001 mm inward correction. The matching water, basin cutter, and support
are checked both before and after STL export. Corrections are recorded under
`water_topology_corrections` in the water integration report.

Water that falls below the 1 mm² printable-area minimum during coordinate cleanup
is omitted and reported with its original and cleaned areas. Builds preserve
original ocean coverage before layer-priority cuts remove bays from it. Generic
water inside that coverage shares one print-layer-aligned sea level with the
ocean, even when trail grooves separate the final pieces. Lakes and upstream
river sections outside that coverage retain shoreline-driven levels. Coastal
classification happens before printable-width cleanup. That cleanup closes
artificial ocean/bay clearance seams only inside the original ocean coverage,
preserving island holes and real land barriers; trail exclusion happens afterward.
Restored area and component counts are recorded under
`coastal_classification.seam_cleanup`. The exported marine
support bottoms receive at most 0.000001 mm of upward separation when pieces
join, preventing coincident bottom sheets without changing the terrain bounds.
The separation is recorded in the coastal report. Marine
surfaces are checked for a common height. Invalid or conflicting coastal source
metadata blocks generation. Remaining duplicate ocean coverage is trimmed with
a 0.001 mm separation from independent inland water. These adjustments
are recorded under `water_overlap_corrections`, and the combined water, cutter,
and support must still pass the STL watertightness checks.

Shoreline smoothing uses a 1.0 mm physical detail scale for the standard 0.4 mm
nozzle workflow. Narrow land tips and tiny piers blend into adjacent water;
narrow dead-end water inlets blend back into land. The generator captures the
original terrain before water and trail cuts. When water is removed, it rebuilds
from that surface and applies the final water basins and trail groove, so old
channels cannot remain underneath. Water contraction is
enabled only when this original terrain is available; legacy low-level callers
without it retain land-only smoothing.
Larger features and islands retain their land cores with rounded outlines where
safe. The model rim, trail-support region, major land connections, and barriers
between independent water bodies are protected. The smoothed outline drives
both water inserts and terrain cuts, so original coastal clipping cannot bring
the tiny details back. Changed area and protected exceptions are recorded under
`shoreline_smoothing`, with restored areas and source checks in `land_restoration`,
in the validation report. Existing exports change only
when rebuilt.

Generation widens terrain grooves from the actual raised trail footprint, targeting
0.12 mm ordinary clearance and 0.27 mm around small enclosed water openings.
The Trail STL is preserved. Water fragments below the existing printable area
or width limits are omitted, leaving terrain in their place. Corrections and
omissions are recorded in the validation report; correction is limited to two
passes and never relaxes the fit requirements.

Generated trails undergo a blocking fit check before replacing deliverables.
If ordinary terrain cleanup leaves exactly one four-face edge no longer than
0.001 mm, repair may collapse that numerical sliver. The correction must preserve
bounds, change volume by at most 0.00001 mm³, and remain watertight with consistent
winding after STL export. Corrections and vertex displacement are recorded in
the repair report; genuine holes and larger defects still block generation.
The check requires 0.1 mm clearance along outer and inner walls, increased to
0.25 mm around enclosed water openings up to 10 mm², with a 0.005 mm numerical
tolerance. A failed check reports the measured gap and model XY location and
preserves previous outputs. Printed fit still depends on printer accuracy.
Details are recorded under `trail_fit`
in the build validation report, including for models without water.

The dashboard uses compact, sticky tabs for Project created, STL generated, 3MF
generated, Printing started, and Print completed. Tab badges show live counts
and the last selected tab is remembered. Each project row can open its complete
folder in Finder. The first three stages follow project files automatically;
use **Start printing**, **Mark completed**, and **Move back** for production.
Manual production state is stored in the project's `workflow.json`.

TrailPrint records normalized project and product identities, scoped status
events, and Pacific end-of-day snapshots as atomic CSV files in
`data/analytics/`. The upload form can mark a project as a test: its complete
history is retained, while Analytics excludes it by default unless **Include
tests** is enabled. A formal raised-map product is created on its first build.
Install the macOS schedule
once with:

```sh
.venv/bin/trailprint analytics-install-scheduler
```

Run the same pipeline manually with `.venv/bin/trailprint analytics-refresh`.
If the repository is stored in a macOS-protected folder such as Desktop or
Documents, the LaunchAgent may also need permission to read that folder. App
startup and the manual refresh command still run the same idempotent catch-up
pipeline safely.

Plate text is entered manually and printed literally. New edge fields start
blank; the title defaults to the readable project name. Existing model text is
restored without conversion or placeholder expansion. The form places Bottom
edge below Title, then Lower left/Lower right and Upper left/Upper right pairs.
Title width is checked against the selected layout before building. Terrain
resolution defaults to 8. App builds use a fixed 1.5 mm terrain-shaped alignment
recess with zero digital clearance where supported. Programmatic builds can
still customize the recess. Plate-generating layouts also default
to three bed-facing recesses for 18.2 mm diameter by 3.0 mm deep dot magnets. The
magnet centers form a 62.354 mm equilateral triangle centered on the backing plate.
The option can be disabled before building, and TrailPrint automatically thickens the
bed-facing backing only as much as needed to retain at least 0.8 mm beneath the
terrain alignment recess.

Missing or unreadable elevation tiles block generation instead of becoming flat
zero-height patches. Mapterhorn tile downloads have bounded retries and use the
provider's supported parent-tile fallback on a 404 above zoom 12. Genuine zero
and negative elevations remain valid. Legacy compiled elevation caches are
bypassed; verified tile coverage and content fingerprints are recorded in the
validation report and a separate versioned cache under the project's build folder.
A failed elevation check preserves existing STL, 3MF, and preview files.
Programmatic builds can set `terrain_elevation_multiplier` for elevation
comparisons; its default is 1.0. The requested exaggeration is applied before
the relief cap, with both requested and effective multipliers recorded in the
validation report. Terrain, the matching trail, and water are rebuilt together;
plate dimensions and water inlay depth retain their configured values.

Terrain cleanup uses at most three passes to resolve numerical overlaps left by
the water Boolean. Each pass records repair counts and remaining non-manifold
edges; the Boolean output is checked before groove correction. Genuine holes,
inconsistent winding, or changes beyond existing volume and bounds tolerances
still block generation and preserve previous deliverables.

Every Blender output is checked before it replaces prior deliverables. Clearly
misplaced meshes are centered using the included Bambu-boundary correction. If
a requested shape extra produces a fragmented terrain or trail, TrailPrint makes
one automatic recovery build with Shape Extras set to None and retains the
original valid plate/text parts. Successful builds open a lightweight local
bird's-eye PNG preview with validation details and an action to open the
project's output folder in Finder. While Blender runs, the settings panel is
replaced by the live build phase and progress. A running build can be cancelled;
TrailPrint stops Blender and discards staged work while preserving prior STL,
3MF, preview, and validation files. Cancelled and failed jobs do not create
analytics status facts. Preview colors use green terrain, red trail, blue water,
black plate, and white text; STL files themselves remain colorless. Builds include
lakes, major rivers, and coastal sea by default as recessed Terrain inlays.
Each inland water body samples the terrain immediately outside its shoreline.
Level shorelines produce a flat two-layer, 0.40 mm inlay; changing shorelines
produce locally quantized 0.20 mm elevation steps while retaining the same
two-layer local depth. Coastal ocean is a flat one-layer, 0.20 mm inlay. Narrow
or pointed regions are widened to at least 1.0 mm before the matching Terrain
basin and terrain-colored support are made. Raw full-layer Ocean geometry is
not packaged.
The separately printed 1.2 mm trail uses 0.10 mm clearance per side in its
terrain groove, for a nominal groove width of 1.4 mm.

From the validated preview, TrailPrint can also create a reviewable Bambu Studio
3MF using the printer, process, filaments, and colors currently configured in
Bambu Studio. Choose filament numbers for each model role and TrailPrint builds
one calibrated plate containing Trail, multipart Terrain + Water when water is
available, and multipart Plate + Text. Objects print by object using the
calibrated layout. Water always uses its selected blue filament as an integrated
part within the Terrain object; it is never independently placed or exported as
a separate plate. Mountain and snow-cap
colors are assigned to Terrain by elevation. An unchecked-by-default DIY kit
option adds a second plate containing an adaptive, print-in-place integrated-knuckle
case. The case reuses the filament assigned to grey terrain rock. The 3MF is validated before
replacing an earlier export. The prime tower is disabled and the purge multiplier
is 0.6, so always confirm the AMS mapping and contamination risk in Bambu Studio
before slicing or printing. During a build, TrailPrint uses
OpenStreetMap forest, rocky-terrain, glacier, and explicitly queried lava-field
polygons as land-cover evidence and fits green, grey, white, and black
observations into at most four elevation bands. This keeps geographic context
while requiring no more than three terrain filament changes. The preview shows
evidence coverage, fitted bands, and confidence, and still offers the original
manual green/grey/white elevation mode. An Overpass failure never blocks STL
output. Plate defaults to black filament slot 1; Trail and Text retain
color-matched defaults.

The plate back is intentionally blank apart from the three dot-magnet recesses.
Include this credit on the product packaging and online listing instead:
`Map data © OpenStreetMap contributors — openstreetmap.org/copyright. Elevation data © Mapterhorn — mapterhorn.com/attribution.`

## Development checks

Install the development tools and run the same checks used by continuous
integration:

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
```
