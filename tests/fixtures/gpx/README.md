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
