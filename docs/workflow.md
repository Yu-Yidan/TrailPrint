# Workflow

The Analytics link in the site header opens a read-only view of all known
project IDs. Status counts include active, soft-deleted, and permanently deleted
projects; the creation chart shows daily new projects using Pacific dates.

1. Create a project in the local interface so it receives the next sortable ID.
2. Confirm the uploaded `route.gpx` and record its source in the project's README.
3. Choose Shape, Shape Extras, plate text, terrain offsets, water options, and
   Resolution on the project page. Elevation scaling is selected automatically.
4. Click **Build with Blender**, or run
   `trailprint build prints/<four-digit-id>-<project-name>`.
5. Inspect the validated STL set in the browser preview.
6. Optionally choose configured Bambu filaments for Trail, Terrain, Plate, Text,
   and generated Water, then create the reviewable 3MF from the preview page.
7. Open the 3MF in Bambu Studio, confirm the AMS mapping and all slicing
   settings, slice, and print.

You can perform steps 1–4 through the local interface by running
`trailprint web`, choosing **New project**, and dropping in a GPX file.

The dashboard groups every active project into one exclusive funnel stage using
sticky tabs with live counts. It remembers the selected tab, and each compact
row includes a control to reveal the full project folder in Finder.
Project created, STL generated, and 3MF generated are derived from deliverables;
Printing started and Print completed are manual states. A project must have its
named 3MF before printing can start. Moving a project back never removes files,
and rebuilding files does not reset an active or completed production state.
Recoverable projects can be restored from **Deleted projects**, or selected in
bulk and permanently removed after typing `DELETE` as confirmation.

While Blender generates STL files, **Cancel build** terminates its complete
process group, including automatic fallback or preview rendering. Cancellation
removes only staged work and preserves previously committed deliverables,
preview, and validation data. It is an operational outcome rather than a funnel
transition, so cancelled and failed jobs do not add fact-table rows.

## Analytics pipeline

TrailPrint maintains these UTF-8 CSV files under `data/analytics/`:

- `dim_project.csv`: project identity, test flag, creation time, and terminal
  permanent-deletion time
- `dim_product.csv`: product identity linked to its source project
- `dim_status.csv`: ordered statuses scoped to projects or a product type
- `dim_project_status_snapshot.csv` and `dim_product_status_snapshot.csv`:
  Pacific end-of-day status rows containing stable IDs only
- `fct_project_status.csv` and `fct_product_status.csv`: normalized transitions
  with UTC `ts`, Pacific `ds`, and status IDs

New projects receive the next integer ID immediately and IDs are never reused.
The integer is formatted with four digits only in folder and deliverable names
(for example, ID `16` becomes `0016-ruoxi-alaska`); IDs above 9999 are rejected
so normal alphabetical sorting remains numerically ordered.
Products are created on their first formal build. The current registered product
type is `fridge_magnet_raised_map`. Test projects receive ordinary
project and product IDs and complete history. Dashboard queries exclude them by
default using `dim_project.dim_is_test`, with an **Include tests** toggle.

Immediate app actions write facts directly. Daily reconciliation synchronizes
the active product lifecycle and writes closed-day project and product
snapshots. CSV rewrites use a process lock and atomic replacement. The macOS LaunchAgent runs
at 12:05 AM and app startup catches up missed closed dates. Run manually with:

```sh
trailprint analytics-refresh
```

Install the per-user schedule with:

```sh
trailprint analytics-install-scheduler
```

macOS may deny a background LaunchAgent access to repositories stored in
protected folders such as Desktop or Documents. Grant the Python executable the
required folder access before installing the schedule, or rely on the identical
startup catch-up plus the manual refresh command. A failed agent should be
unloaded rather than left retrying.

`dim_status.csv` is never changed by the scheduled pipeline. Whenever a funnel
step is added, renamed, or removed, update this table manually. The homepage
shows a non-blocking warning when an application status is missing from it.

`build/` is disposable scratch space. Final STL and 3MF files are versioned
through Git LFS; install Git LFS before committing binary deliverables.

## Generation settings

The local interface intentionally exposes a small subset of TrailPrint3D:

- Shape and shape-specific extras, defaulting to Outer text where supported
- Plate title and five editable fields showing the literal text that will print
- A readable literal title with Arial Black physical-width validation for the
  selected text layout
- A shape-aware diagram showing where each plate text field is placed
- Trip date from the first GPX timestamp, formatted as `YYYY.MM.DD` and shown
  literally in Text 2
- Country and state detected asynchronously from the first GPX point, shown in
  editable Text 4 and Text 5 fields respectively, and cached in `location.json`
- X and Y terrain offsets
- Expected printed relief calculated from GPX elevations; routes above 15 mm
  are automatically limited to 15 mm of relief (about 17 mm with the base)
- A terrain-shaped plate recess, defaulting to 1.5 mm deep with zero digital
  clearance; available only for layouts that generate a separate plate
- Terrain resolution from 1–10 (8 by default; 8 or lower recommended)
- Single-color trail mode, which is always enabled

Build choices are session-only and reset when the project page reloads. Text 4
and Text 5 location corrections are saved when a build is submitted. Location
detection sends the first route coordinate to OpenStreetMap Nominatim; failure
leaves editable blank fields and never blocks a build. All other settings use
the defaults supplied by the installed TrailPrint3D add-on. Blender runs
invisibly, downloads terrain data, and exports STL geometry only.

## Validation and recovery

Generated STL files are staged and parsed before they replace anything in
`deliverables/`. TrailPrint checks that each file has usable geometry and that
the trail is dominated by one connected route rather than floating fragments.
Small non-manifold or degenerate artifacts are reported as warnings because
they can occur in otherwise usable TrailPrint3D output.

Files with an obviously incorrect export origin are centered in X/Y and moved
to Z=0 using the same operations as `fix_bambu_plate_boundary.py`. Centered
files are left unchanged. If terrain or trail validation fails while a Shape
Extra is selected, one fallback generation runs with Shape Extras set to None.
Valid fallback terrain/trail files are combined with the originally requested
plate/text and validated again. Previous deliverables remain untouched if the
final set fails.

The complete report is stored in `build/validation.json`. Successful builds
open a local bird's-eye PNG preview with part names, dimensions, warnings, and
an explicit notice when automatic recovery was used. The preview page can open
the project's `deliverables/` folder directly in Finder. This preview validates
geometry, not Bambu printer profiles, slicing, supports, or G-code.

## Bambu Studio project export

The preview page reads the currently selected printer, process, filament names,
and filament colors from the local Bambu Studio configuration. A successful
Blender build also records the downloaded terrain's real elevation range and
model-Z mapping; older builds must be rebuilt once before elevation colors are
available. TrailPrint creates an
unsliced `deliverables/<project-name>.3mf` with this plate layout:

1. Trail
2. Terrain
3. Plate + Text as one object containing two independently assignable parts
4. Water, when generated and left in the default separate mode, as blue recessed
   inserts in one multipart object

When OpenStreetMap land-cover evidence is reachable during the terrain build,
the preview defaults to OSM-inspired colors. Forest and otherwise unclassified
land are green; scree and rock are grey; glaciers are white; and only explicitly
tagged lava fields or flows are black. TrailPrint approximates those geographic
polygons with at most four contiguous elevation bands. The optimizer charges an
8% mismatch penalty per additional transition, so it uses fewer colors when
extra swaps add little value. Evidence coverage and match confidence make the
approximation reviewable. If Overpass is unavailable, STL generation still
succeeds and manual mode remains available.

Manual mode offers Land, Mountain, and Summit filament selectors, defaulting to the
closest configured green, neutral-grey, and white colors. Mountain begins at
1,200 m and Summit at 2,300 m by default. Imperial projects display and accept
cutoffs in feet; Metric projects use meters. The cutoffs and filament slots are
editable and can be saved as local defaults. Terrain that never reaches the
mountain cutoff remains green, terrain below the summit cutoff uses green and
grey, and higher terrain uses all three; bands thinner than one configured
print layer are omitted. Plate defaults to slot 3. If slot 3 is not configured,
Plate uses the highest available slot. Trail and Text choose the closest
configured color to their preview colors. After a successful export,
the deliverables folder opens in Finder and the browser returns to the project
dashboard.

Lakes, major rivers, and coastal sea share one water option that is enabled by
default; minor waterways are omitted to avoid fragile pieces. Regions smaller
than 25 mm² on the finished model are filtered before geometry and recesses are
created. Water uses TrailPrint3D's recessed single-color geometry and
prints alone on its plate, avoiding water-to-terrain filament swaps. If no usable
water data is returned, the build succeeds and retains the three-plate layout.
The Bambu export form can instead place Water in the Terrain multipart assembly.
That produces three plates and preserves the fitted recesses, but requires AMS or
repeated mid-layer manual swaps wherever blue water and terrain share a layer.
TrailPrint records the estimated affected layer ranges and an upper-bound swap
count using the selected process's initial and nominal layer heights. This is an
unsliced estimate; variable layer height and other slicer changes can alter the
exact result shown by Bambu Studio after slicing.

TrailPrint disables the prime tower and writes zero for all purge volumes. This
reduces waste but can cause visible color contamination at terrain transitions
and on the multipart third plate. The result must be opened and reviewed in Bambu Studio;
TrailPrint does not automatically slice, send, or start a printer job. A new 3MF
only replaces the previous one after its archive, plate layout, multipart
structure, filament assignments, prime-tower setting, and purge matrix validate.
