"""TrailPrint command-line interface."""

import argparse
import sys
from pathlib import Path

from .blender_bridge import BuildSettings, SHAPE_EXTRAS, run_blender_build
from .config import load_config
from .geometry import ribbon_mesh
from .gpx import project, read_gpx
from .mesh import write_stl


def build_ribbon(print_dir: Path, presets_dir: Path) -> Path:
    print_dir = print_dir.resolve()
    config = load_config(print_dir, presets_dir.resolve())
    points = project(read_gpx(print_dir / "route.gpx"), float(config["max_size_mm"]),
                     float(config["elevation_scale"]))
    vertices, faces = ribbon_mesh(points, float(config["track_width_mm"]),
                                  float(config["base_thickness_mm"]))
    output = print_dir / "deliverables" / f"{print_dir.name}.stl"
    write_stl(output, vertices, faces, print_dir.name)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trailprint")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build one print project")
    build_parser.add_argument("print_directory", type=Path)
    build_parser.add_argument("--shape", default="HEXAGON", choices=SHAPE_EXTRAS)
    build_parser.add_argument("--shape-extra", default="NONE")
    build_parser.add_argument("--resolution", default=4, type=int)
    web_parser = subparsers.add_parser("web", help="run the local project interface")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args(argv)
    if args.command == "web":
        from .web import create_app
        create_app().run(host=args.host, port=args.port)
        return 0
    try:
        settings = BuildSettings.validated(args.shape, args.shape_extra, args.resolution)
        outputs = run_blender_build(args.print_directory, settings)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for output in outputs:
        print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
