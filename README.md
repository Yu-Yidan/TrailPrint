# TrailPrint

TrailPrint uses EmGi's TrailPrint3D Blender add-on to turn a GPX track into a
printable terrain map for Bambu Studio. Each design lives in
`prints/<four-digit-id>-<slug>/`
with its source, scratch build data, and final deliverables.

The local Analytics page summarizes project IDs by workflow status and plots
daily project creation in Pacific time without an external chart service.

## Quick start

Python 3.9+, Blender 5.2, and an enabled TrailPrint3D add-on are required.

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
enabling water and single-color trail mode. Terrain expected to exceed 15 mm of
printed relief is automatically limited to 15 mm. Projects can be
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

Text 2 shows the GPX trip date in `YYYY.MM.DD` format. Country and state are
detected in the background from the first GPX point using OpenStreetMap
Nominatim, shown directly in editable Text 4 and Text 5 fields respectively,
and cached per project. Title defaults to the readable project name and is
measured against the selected plate layout before a build can start. Terrain
resolution defaults to 8. Plate-generating layouts default to an adjustable
1.5 mm terrain-shaped alignment recess with zero digital clearance.

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
lakes, major rivers, and coastal sea by default as recessed inserts. Water
regions smaller than 25 mm² on the model are filtered out before
their matching recesses are made.

From the validated preview, TrailPrint can also create a reviewable Bambu Studio
3MF using the printer, process, filaments, and colors currently configured in
Bambu Studio. Choose filament numbers for each model role and TrailPrint builds
one calibrated plate containing Trail, multipart Terrain + Water when water is
available, and multipart Plate + Text. Objects print by object using the
calibrated layout. Water always uses its selected blue filament within the
Terrain object; it is never exported as a separate plate. Snow-cap projects are
currently blocked from this one-plate export. The 3MF is validated before
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

## Development checks

Install the development tools and run the same checks used by continuous
integration:

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest -q
```
