"""TrailPrint command-line interface."""

import argparse
import sys
from pathlib import Path

from .blender_bridge import SHAPE_EXTRAS, BuildSettings, run_blender_build
from .naming import human_name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trailprint")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build one print project")
    build_parser.add_argument("print_directory", type=Path)
    build_parser.add_argument("--shape", default="HEXAGON", choices=SHAPE_EXTRAS)
    build_parser.add_argument("--shape-extra", default="NONE")
    build_parser.add_argument("--resolution", default=8, type=int)
    web_parser = subparsers.add_parser("web", help="run the local project interface")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args(argv)
    if args.command == "web":
        from .web import create_app
        create_app().run(host=args.host, port=args.port)
        return 0
    try:
        settings = BuildSettings.validated(args.shape, args.shape_extra, args.resolution,
                                           plate_title=human_name(args.print_directory))
        outputs = run_blender_build(args.print_directory, settings)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for output in outputs:
        print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
