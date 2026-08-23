# Workflow

1. Copy `prints/example-trail` to a lowercase, hyphenated project name.
2. Replace `route.gpx` and record its source in the project's README.
3. Choose Shape, Shape Extras, and Resolution on the project page.
4. Click **Build with Blender**, or run `trailprint build prints/<project-name>`.
5. Inspect the STL in `deliverables/`, then import it into Bambu Studio.
6. Choose orientation, printer, filament, supports, and slicing settings.
7. Save the Bambu project as `deliverables/<project-name>.3mf` and add a preview.

You can perform steps 1–4 through the local interface by running
`trailprint web`, choosing **New project**, and dropping in a GPX file.

`build/` is disposable scratch space. Final STL and 3MF files are versioned
through Git LFS; install Git LFS before committing binary deliverables.

## Generation settings

The local interface intentionally exposes a small subset of TrailPrint3D:

- Shape and shape-specific extras
- Terrain resolution from 1–10 (4 by default; 8 or lower recommended)
- Single-color trail mode, which is always enabled

These choices are session-only and reset when the project page reloads. All
other settings use the defaults supplied by the installed TrailPrint3D add-on.
Blender runs invisibly, downloads terrain data, and exports STL geometry only.
