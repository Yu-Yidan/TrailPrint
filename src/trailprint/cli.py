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
    analytics_parser = subparsers.add_parser(
        "analytics-refresh", help="reconcile events and write the prior Pacific daily snapshot")
    analytics_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    scheduler_parser = subparsers.add_parser(
        "analytics-install-scheduler", help="install the daily macOS launchd analytics job")
    scheduler_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.command == "web":
        from .web import create_app
        create_app().run(host=args.host, port=args.port)
        return 0
    if args.command == "analytics-refresh":
        from .analytics import AnalyticsStore
        root = args.repo_root.resolve()
        snapshot = AnalyticsStore(root).run_daily(root / "prints", root / ".trailprint-trash")
        print(f"Wrote Pacific end-of-day snapshot for {snapshot}")
        return 0
    if args.command == "analytics-install-scheduler":
        from .analytics import install_launch_agent
        target = install_launch_agent(args.repo_root.resolve(), Path(sys.argv[0]).resolve())
        print(f"Installed {target}")
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
