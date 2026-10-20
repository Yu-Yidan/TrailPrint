# GPX test fixtures

Place representative `.gpx` files in this folder when you want them used for
future TrailPrint3D fixes and regression checks.

Keep filenames descriptive, for example:

- `hawaii-test.gpx`
- `short-flat-trail.gpx`
- `steep-slope.gpx`

These files are test inputs only and are not treated as production projects.

`triathlon/` contains three synthetic, non-personal recordings near Lake Tahoe.
The swim is offshore and disconnected from the land routes. Bike and run share
an endpoint and part of their footprint. Their timestamps contain gaps between
recordings, which must not add distance, elevation gain, or duration. These are
geometry regression inputs, not navigation routes or actual race recordings.

`newport-shoreline.gpx` preserves the three Newport course geometries that
exposed shoreline sampling entirely covered by a trail groove. Track names,
timestamps, and extensions are removed; only coordinates and elevations remain.

`michigan-water-loop.gpx` preserves the three Michigan course segments whose
small enclosed water opening exposed insufficient inner-wall clearance. Only
coordinates and elevations are retained; names, timestamps, and extensions are
removed. Use the saved model layout settings for route-specific build checks.

`javelina-clearance.gpx` preserves the Javelina route that exposed 0.078 mm
outer-wall clearance despite a nominal 0.100 mm groove tolerance. Only coordinates
and elevations are retained; personal metadata is removed.

`high-country-water.gpx` preserves the High Country course that exposed
point contacts around lake islands during water shell generation. Only
coordinates and elevations are retained. The corresponding minimal shoreline
regressions are stored in `../high-country-water-polygons.json` in model millimetres.

`ledford-branch.gpx` retains only coordinates and elevations from project
4205711750 for water-generation regression builds. Small waterways remain
excluded by the generator.

`../water-terrain-overlap.npz` contains captured terrain Boolean vertices and
triangle indices. It reproduces overlapping bottom facets requiring a second
repair pass and retains shared terrain-repair regression coverage.

`underground-rr.gpx` retains only the coordinates and elevations of the route
that exposed disconnected curve-export artifacts. Track names, timestamps and
extensions are removed. `../fragmented-trail-artifacts.npz` captures its generated
trail vertices and triangle indices in model millimetres, reproducing 749 face
components without requiring Blender for the focused regression.

`../groove-coplanar-floor.npz` captures the Underground RR terrain after a
coplanar groove Boolean. Its seven-edge floor opening must remain a blocking
error when inspected directly; generation avoids creating it by separating the
cutter floor at STL precision. The fixture contains only model vertices and
triangle indices, without project metadata.

`../trail-projection-ring-edge.npz` captures terrain vertices, face order and
trail floor height that reproduce GEOS's `Ring edge missing` error during the
raised-material projection. The mesh is in model millimetres; it contains no
project metadata. Its individually valid projected triangles exercise the
bounded precision retry without weakening trail clearance checks.
