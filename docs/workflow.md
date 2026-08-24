# Workflow

1. Copy `prints/example-trail` to a lowercase, hyphenated project name.
2. Replace `route.gpx` and record its source in the project's README.
3. Choose Shape, Shape Extras, plate text, terrain offsets, elevation scale,
   and Resolution on the project page.
4. Click **Build with Blender**, or run `trailprint build prints/<project-name>`.
5. Inspect the validated STL set in the browser preview.
6. Optionally choose a configured Bambu filament for Trail, Terrain, Plate, and
   Text, then create the reviewable three-plate 3MF from the preview page.
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

- `dim_project.csv`: latest project name, slug, creation time, and terminal
  permanent-deletion time; one durable row per sequential `id_project`
- `dim_status.csv`: manually maintained status identifiers and names
- `dim_project_status.csv`: one Pacific end-of-day row per project that had not
  been permanently deleted by that day-end
- `fct_project_status.csv`: every status transition, with UTC `ts`, Pacific
  `ds`, and the project name used when the event occurred

New projects receive the next integer ID immediately and IDs are never reused.
Existing projects are initialized once by filesystem creation time, with slug
as a deterministic tie-breaker. No historical facts are backfilled. A project
created and permanently deleted on the same Pacific day is absent from the
daily snapshot, but both events remain in the fact table. Soft-deleted projects
remain in snapshots until restored or permanently deleted.

Immediate app actions write facts directly. The daily reconciliation records
backward transitions caused by manually removed STL or 3MF files, then rebuilds
the closed day's snapshot by replaying facts through Pacific midnight. CSV
rewrites use a process lock and atomic replacement. The macOS LaunchAgent runs
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
- Optional fixed 10 mm elevation scale
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
and filament colors from the local Bambu Studio configuration. It creates an
unsliced `deliverables/<project-name>.3mf` with this plate layout:

1. Trail
2. Terrain
3. Plate + Text as one object containing two independently assignable parts

Terrain defaults to filament slot 1 and Plate to slot 3. If slot 3 is not
configured, Plate uses the highest available slot. Trail and Text choose the
closest configured color to their preview colors. After a successful export,
the deliverables folder opens in Finder and the browser returns to the project
dashboard.

TrailPrint disables the prime tower and writes zero for all purge volumes. This
reduces waste but can cause visible color contamination, especially on the
multipart third plate. The result must be opened and reviewed in Bambu Studio;
TrailPrint does not automatically slice, send, or start a printer job. A new 3MF
only replaces the previous one after its archive, plate layout, multipart
structure, filament assignments, prime-tower setting, and purge matrix validate.
