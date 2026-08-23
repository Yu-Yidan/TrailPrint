# TrailPrint

TrailPrint uses EmGi's TrailPrint3D Blender add-on to turn a GPX track into a
printable terrain map for Bambu Studio. Each design lives in `prints/<slug>/`
with its source, scratch build data, and final deliverables.

## Quick start

Python 3.9+, Blender 5.2, and an enabled TrailPrint3D add-on are required.

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/trailprint build prints/example-trail
```

The command launches Blender in the background and writes TrailPrint3D's STL
files to `prints/example-trail/deliverables/`. Import them into Bambu Studio,
choose the printer and slicing settings, then save the 3MF project there.

See [docs/workflow.md](docs/workflow.md) for the complete workflow.

## Local interface

Start the project dashboard and open <http://127.0.0.1:5000>:

```sh
.venv/bin/trailprint web
```

The interface lists existing projects, accepts GPX files by drag-and-drop, and
can run TrailPrint3D from a project detail page. The reduced interface exposes
shape, shape extras, and resolution while always enabling single-color trail
mode. Run the command from the repository root so it can find `prints/`.
