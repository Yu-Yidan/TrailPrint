# GPX test fixtures

Place representative `.gpx` files in this folder when you want them used for
future TrailPrint3D fixes and regression checks.

Keep filenames descriptive, for example:

- `hawaii-test.gpx`
- `short-flat-trail.gpx`
- `steep-slope.gpx`

These files are test inputs only and are not treated as production projects.

`tomales-water-precision.gpx` preserves only the coordinates and elevations of
the route whose tiny coastal water fragment crossed the printable-area threshold
during precision cleanup. Names, timestamps, and extensions are removed.
`../tomales-water-precision.wkt` captures that pre-cleanup polygon in model
millimetres: cleanup reduces its area from 1.0094 to 0.9989 mm² without splitting it.
`../tomales-water-overlap.json` contains model-space inland and ocean polygons
whose overlapping floor faces become non-manifold when combined in an STL.

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

`crosstown-tiny-edge.gpx` retains only SF Crosstown route coordinates and
elevations, without names, timestamps, or extensions. `../crosstown-tiny-edge.npz`
captures its groove-corrected terrain in model millimetres. Ordinary repair
leaves one 0.000184 mm edge shared by four faces; it exercises the guarded
tiny-edge collapse and binary STL round-trip checks.
The same route also exercises coastal water classification: bay polygons in
the generic water layer must share the captured ocean plane after regeneration.
Its small piers also exercise print-scale shoreline smoothing while retaining
the original trail, protected shoreline support, and continuous marine surface.
The Tomales route additionally checks that independent inland water retains
shoreline-based levels while marine fragments share one sea level.

`../trail-projection-ring-edge.npz` captures terrain vertices, face order and
trail floor height that reproduce GEOS's `Ring edge missing` error during the
raised-material projection. The mesh is in model millimetres; it contains no
project metadata. Its individually valid projected triangles exercise the
bounded precision retry without weakening trail clearance checks.

`big-dam-bridge-base.gpx` retains only route coordinates and elevations, without names, timestamps or extensions. Its water Boolean exposes mismatched flat underside triangulation; the verified pre-cut base must be restored without altering intended openings.

`chicago-lake-width.gpx` contains only Chicago lakefront route coordinates and elevations. It exercises thin lake sections that must be widened even when the same body already contains a wide area.
