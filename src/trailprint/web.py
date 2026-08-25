"""Local browser interface for TrailPrint."""

import subprocess
from dataclasses import replace
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, url_for

from .blender_bridge import BuildSettings, EXTRA_LABELS, SHAPE_EXTRAS, SHAPE_LABELS
from .bambu import BambuSettings, default_filament_mapping, discover_environment
from .bambu_jobs import BambuJobManager
from .analytics import AnalyticsStore, effective_status
from .config import load_config
from .gpx import (AUTO_RELIEF_LIMIT_MM, estimate_print_relief_mm,
                  format_trip_stats, read_gpx_trip_stats, resolve_trip_text)
from .jobs import BuildBusy, BuildManager
from .location import detect_location, has_location_cache, read_location, save_location
from .mesh_validation import mesh_role, read_report
from .naming import canonical_3mf_name, human_name
from .terrain_colors import (display_elevation, elevation_metadata,
                             elevation_to_meters, load_terrain_defaults,
                             save_terrain_preferences)
from .osm_colors import load_palette
from .projects import (FUNNEL_STAGES, create_project, list_deleted_projects, list_projects,
                       migrate_active_project, migrate_active_projects,
                       permanently_delete_projects, purge_trash, read_upload,
                       resolve_project, restore_project,
                       trash_project, update_production_status)
from .title import TITLE_LIMITS_MM, resolve_project_title, validate_title_fit


def create_app(repo_root: Path = None, build_manager: BuildManager = None,
               bambu_manager: BambuJobManager = None) -> Flask:
    root = (repo_root or Path.cwd()).resolve()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="trailprint-local-only",
        MAX_CONTENT_LENGTH=10 * 1024 * 1024 + 64 * 1024,
        REPO_ROOT=root,
        PRINTS_DIR=root / "prints",
        PRESETS_DIR=root / "presets",
        TRASH_DIR=root / ".trailprint-trash",
        ANALYTICS_DIR=root / "data/analytics",
    )
    app.extensions["trailprint_builds"] = build_manager or BuildManager()
    app.extensions["trailprint_bambu"] = bambu_manager or BambuJobManager()
    analytics = AnalyticsStore(root)
    app.extensions["trailprint_analytics"] = analytics
    analytics.run_daily(app.config["PRINTS_DIR"], app.config["TRASH_DIR"])
    migrate_active_projects(app.config["PRINTS_DIR"])
    purge_trash(app.config["TRASH_DIR"], retention_days=30,
                before_remove=analytics.mark_permanently_deleted)

    @app.get("/")
    def dashboard():
        for path in app.config["PRINTS_DIR"].iterdir() if app.config["PRINTS_DIR"].is_dir() else []:
            if path.is_dir() and not path.name.startswith("."):
                analytics.sync_project(path)
        projects = list_projects(app.config["PRINTS_DIR"])
        grouped = {key: [] for key, _label in FUNNEL_STAGES}
        for project in projects:
            grouped[project.stage].append(project)
        default_stage = next((key for key, _label in FUNNEL_STAGES if grouped[key]), "created")
        return render_template(
            "dashboard.html", projects=projects, grouped_projects=grouped,
            funnel_stages=FUNNEL_STAGES, default_stage=default_stage,
            deleted_count=len(list_deleted_projects(app.config["TRASH_DIR"])),
            analytics_status_warnings=analytics.status_warnings())

    @app.get("/analytics")
    def analytics_dashboard():
        for path in app.config["PRINTS_DIR"].iterdir() if app.config["PRINTS_DIR"].is_dir() else []:
            if path.is_dir() and not path.name.startswith("."):
                analytics.sync_project(path)
        return render_template(
            "analytics.html",
            analytics_data=analytics.visualization_data(
                app.config["PRINTS_DIR"], app.config["TRASH_DIR"]))

    @app.get("/deleted-projects")
    def deleted_projects():
        return render_template(
            "deleted_projects.html",
            projects=list_deleted_projects(app.config["TRASH_DIR"]))

    @app.post("/deleted-projects/<entry_name>/restore")
    def restore_deleted_project(entry_name):
        try:
            project = restore_project(
                app.config["PRINTS_DIR"], app.config["TRASH_DIR"], entry_name)
            analytics.transition(project, effective_status(project))
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("deleted_projects"))
        flash(f"Restored '{project.name}'.", "success")
        return redirect(url_for("project_detail", slug=project.name))

    @app.post("/deleted-projects/permanent-delete")
    def permanently_delete_deleted_projects():
        try:
            removed = permanently_delete_projects(
                app.config["TRASH_DIR"], request.form.getlist("entries"),
                request.form.get("confirmation", ""),
                before_remove=analytics.mark_permanently_deleted)
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Permanently deleted {len(removed)} project(s). This cannot be undone.", "success")
        return redirect(url_for("deleted_projects"))

    @app.get("/projects/new")
    def new_project():
        return render_template("new_project.html")

    @app.post("/projects")
    def save_project():
        name = request.form.get("name", "").strip()
        upload = request.files.get("gpx")
        try:
            if upload is None:
                raise ValueError("Choose a GPX file.")
            data = read_upload(upload.stream, upload.filename or "")
            analytics.next_project_id()
            project = create_project(
                app.config["PRINTS_DIR"], app.config["PRESETS_DIR"], name, data)
            analytics.register_project(project, name)
            project = migrate_active_project(project)
        except (OSError, ValueError) as exc:
            return render_template("new_project.html", error=str(exc), name=name), 400
        flash("Project created successfully.", "success")
        return redirect(url_for("project_detail", slug=project.name))

    def get_project(slug: str) -> Path:
        try:
            project = resolve_project(app.config["PRINTS_DIR"], slug)
        except ValueError:
            abort(404)
        if not project.is_dir():
            abort(404)
        return project

    @app.get("/projects/<slug>")
    def project_detail(slug):
        project = get_project(slug)
        analytics.sync_project(project)
        deliverables = project / "deliverables"
        job = app.extensions["trailprint_builds"].for_project(project)
        location = read_location(project)
        stats = read_gpx_trip_stats(project / "route.gpx")
        expected_relief = estimate_print_relief_mm(project / "route.gpx")
        trip_values = {
            "IMPERIAL": format_trip_stats(stats, "IMPERIAL"),
            "METRIC": format_trip_stats(stats, "METRIC"),
        }
        preview_path = project / "build/preview.png"
        return render_template(
            "project.html", project=project,
            project_title=human_name(project),
            has_gpx=(project / "route.gpx").is_file(),
            stl_files=sorted(path.name for path in deliverables.glob("*.stl")) if deliverables.is_dir() else [],
            shapes=SHAPE_LABELS, extras=SHAPE_EXTRAS, extra_labels=EXTRA_LABELS,
            job=job, location=location, has_location_cache=has_location_cache(project),
            trip_values=trip_values,
            expected_relief=expected_relief,
            relief_limit=AUTO_RELIEF_LIMIT_MM,
            plate_title=human_name(project), title_limits=TITLE_LIMITS_MM,
            has_preview=preview_path.is_file(),
            validation=read_report(project / "build/validation.json"))

    @app.post("/projects/<slug>/location")
    def project_location(slug):
        project = get_project(slug)
        try:
            location = detect_location(project)
        except (OSError, ValueError) as exc:
            return jsonify({"status": "failed", "error": str(exc)}), 502
        return jsonify({"status": "resolved", **location})

    @app.post("/projects/<slug>/build")
    def build_project(slug):
        project = get_project(slug)
        try:
            expected_relief = estimate_print_relief_mm(project / "route.gpx")
            limit_elevation = (expected_relief is not None
                               and expected_relief > AUTO_RELIEF_LIMIT_MM)
            settings = BuildSettings.validated(
                request.form.get("shape", "HEXAGON"), request.form.get("shape_extra", "OUTER TEXT"),
                request.form.get("resolution", 8), request.form.get("plate_title", "{name}"),
                request.form.get("plate_text_1", "{length}"),
                request.form.get("plate_text_2", "{date}"),
                request.form.get("plate_text_3", "{duration}"),
                request.form.get("plate_text_4", "{country}"),
                request.form.get("plate_text_5", "{state}"),
                request.form.get("x_offset", 0), request.form.get("y_offset", 0),
                limit_elevation,
                request.form.get("city", ""), request.form.get("state", ""),
                request.form.get("country", ""),
                "METRIC" if request.form.get("use_metric") == "on" else "IMPERIAL",
                request.form.get("plate_recess_depth", 1.5),
                request.form.get("include_water") == "on")
            stats = read_gpx_trip_stats(project / "route.gpx")
            replacements = {"name": human_name(project), "city": settings.city,
                            "state": settings.state, "country": settings.country}
            settings = replace(settings,
                plate_title=resolve_trip_text(settings.plate_title, stats, settings.unit_system, **replacements),
                plate_text_1=resolve_trip_text(settings.plate_text_1, stats, settings.unit_system, **replacements),
                plate_text_2=resolve_trip_text(settings.plate_text_2, stats, settings.unit_system, **replacements),
                plate_text_3=resolve_trip_text(settings.plate_text_3, stats, settings.unit_system, **replacements),
                plate_text_4=resolve_trip_text(settings.plate_text_4, stats, settings.unit_system, **replacements),
                plate_text_5=resolve_trip_text(settings.plate_text_5, stats, settings.unit_system, **replacements))
            resolved_title = resolve_project_title(settings.plate_title, human_name(project))
            validate_title_fit(resolved_title, settings.shape, settings.shape_extra)
            settings = replace(settings, plate_title=resolved_title)
            save_location(project, settings.city, settings.state, settings.country)
            job = app.extensions["trailprint_builds"].start(project, settings)
        except (BuildBusy, OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("TrailPrint3D build started in Blender.", "success")
        return redirect(url_for("project_detail", slug=slug, job=job.id if 'job' in locals() else None))

    @app.post("/projects/<slug>/delete")
    def delete_project(slug):
        project = get_project(slug)
        try:
            if app.extensions["trailprint_builds"].is_active(project):
                raise ValueError("This project is currently building. Wait for the build to finish before deleting it.")
            if app.extensions["trailprint_bambu"].is_active(project):
                raise ValueError("This project is currently exporting. Wait for the Bambu project to finish before deleting it.")
            analytics.sync_project(project)
            deleted = trash_project(app.config["PRINTS_DIR"], app.config["TRASH_DIR"], slug,
                                    request.form.get("confirmation", ""))
            analytics.transition(deleted, "project soft deleted")
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Moved '{slug}' to local trash. It can be recovered for 30 days.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/projects/<slug>/production-status")
    def production_status(slug):
        project = get_project(slug)
        try:
            analytics.sync_project(project)
            update_production_status(project, request.form.get("action", ""))
            analytics.sync_project(project)
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Production status updated.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/builds/<job_id>")
    def build_status(job_id):
        job = app.extensions["trailprint_builds"].get(job_id)
        if job is None:
            abort(404)
        if job.state == "success":
            analytics.sync_project(job.project)
        return jsonify(app.extensions["trailprint_builds"].serialize(job))

    @app.post("/builds/<job_id>/cancel")
    def cancel_build(job_id):
        try:
            job = app.extensions["trailprint_builds"].cancel(job_id)
        except ValueError as exc:
            return jsonify({"status": "error", "error": str(exc)}), 409
        return jsonify(app.extensions["trailprint_builds"].serialize(job))

    @app.get("/projects/<slug>/files/<filename>")
    def download_file(slug, filename):
        project = get_project(slug)
        if (Path(filename).name != filename
                or Path(filename).suffix.lower() not in {".stl", ".3mf"}):
            abort(404)
        return send_from_directory(project / "deliverables", filename, as_attachment=True)

    @app.get("/projects/<slug>/preview")
    def preview_project(slug):
        project = get_project(slug)
        analytics.sync_project(project)
        deliverables = project / "deliverables"
        files = sorted(deliverables.glob("*.stl")) if deliverables.is_dir() else []
        if not files or not (project / "build/preview.png").is_file():
            flash("Build the project before opening its preview.", "error")
            return redirect(url_for("project_detail", slug=slug))
        preview_files = [{
            "name": path.name, "role": mesh_role(path, slug),
        } for path in files]
        available_roles = {item["role"] for item in preview_files}
        missing_roles = [role for role in ("trail", "terrain", "plate", "text")
                         if role not in available_roles]
        try:
            bambu_environment = discover_environment()
            bambu_error = ""
            filament_defaults = default_filament_mapping(bambu_environment.filaments)
            terrain_defaults = load_terrain_defaults(
                root, project, bambu_environment.filaments)
            filament_defaults.update(terrain_defaults)
        except ValueError as exc:
            bambu_environment, filament_defaults, terrain_defaults, bambu_error = None, {}, {}, str(exc)
        elevation = elevation_metadata(project)
        unit_system = str(elevation.get("unit_system", "IMPERIAL")).upper()
        elevation_view = ({
            "minimum": display_elevation(elevation["source_min_m"], unit_system),
            "maximum": display_elevation(elevation["source_max_m"], unit_system),
            "grey_start": display_elevation(terrain_defaults.get("grey_start_m", 1200), unit_system),
            "white_start": display_elevation(terrain_defaults.get("white_start_m", 2300), unit_system),
            "unit": "m" if unit_system == "METRIC" else "ft",
            "unit_system": unit_system,
        } if elevation else {})
        osm_palette = load_palette(project)
        if osm_palette and elevation:
            osm_palette = {**osm_palette, "display_bands": [{
                **band,
                "display_min": display_elevation(band["min_elevation_m"], unit_system),
                "display_max": display_elevation(band["max_elevation_m"], unit_system),
            } for band in osm_palette["bands"]]}
        bambu_job = app.extensions["trailprint_bambu"].for_project(project)
        return render_template(
            "preview.html", project=project, files=preview_files,
            project_title=human_name(project),
            validation=read_report(project / "build/validation.json"),
            bambu_environment=bambu_environment, bambu_defaults=filament_defaults,
            bambu_error=bambu_error, bambu_job=bambu_job,
            elevation=elevation_view,
            osm_palette=osm_palette,
            bambu_missing_roles=missing_roles,
            bambu_ready=not missing_roles and bool(elevation),
            bambu_output=(project / "deliverables" / canonical_3mf_name(project)).is_file())

    @app.post("/projects/<slug>/bambu")
    def build_bambu_project(slug):
        project = get_project(slug)
        try:
            environment = discover_environment()
            elevation = elevation_metadata(project)
            if not elevation:
                raise ValueError("Rebuild the terrain before creating elevation colors.")
            unit_system = str(elevation.get("unit_system", "IMPERIAL")).upper()
            grey_start_m = elevation_to_meters(
                request.form.get("grey_start"), unit_system)
            white_start_m = elevation_to_meters(
                request.form.get("white_start"), unit_system)
            terrain_mode = request.form.get("terrain_mode", "manual")
            palette = load_palette(project) if terrain_mode == "osm" else {}
            settings = BambuSettings.validated(
                len(environment.filaments), request.form.get("trail_filament"),
                request.form.get("land_filament"), request.form.get("mountain_filament"),
                request.form.get("summit_filament"), request.form.get("plate_filament"),
                request.form.get("text_filament"), request.form.get("water_filament", 1),
                grey_start_m, white_start_m,
                request.form.get("black_filament", 1),
                request.form.get("green_filament", request.form.get("land_filament", 1)),
                request.form.get("grey_filament", request.form.get("mountain_filament", 1)),
                request.form.get("white_filament", request.form.get("summit_filament", 1)),
                terrain_mode, palette.get("bands", ()))
            color_preferences = {
                **settings.terrain_filaments(),
                "grey_start_m": grey_start_m,
                "white_start_m": white_start_m,
            }
            if any(mesh_role(path, project.name) == "water"
                   for path in (project / "deliverables").glob("*.stl")):
                color_preferences["water"] = settings.water_filament
            save_terrain_preferences(
                root, project, color_preferences,
                save_defaults=request.form.get("save_terrain_defaults") == "on")
            job = app.extensions["trailprint_bambu"].start(project, settings)
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Bambu Studio project export started.", "success")
        return redirect(url_for("preview_project", slug=slug,
                                bambu_job=job.id if 'job' in locals() else None))

    @app.get("/bambu-builds/<job_id>")
    def bambu_build_status(job_id):
        job = app.extensions["trailprint_bambu"].get(job_id)
        if job is None:
            abort(404)
        if job.state == "success":
            analytics.sync_project(job.project)
        return jsonify(app.extensions["trailprint_bambu"].serialize(job))

    @app.post("/projects/<slug>/open-bambu-project")
    def open_bambu_project(slug):
        project = get_project(slug)
        output = project / "deliverables" / canonical_3mf_name(project)
        if not output.is_file():
            flash("Create the Bambu project before opening it.", "error")
        else:
            try:
                subprocess.Popen(["open", "-a", "BambuStudio", str(output)], start_new_session=True)
            except OSError as exc:
                flash(f"Could not open Bambu Studio: {exc}", "error")
        return redirect(url_for("preview_project", slug=slug))

    @app.get("/projects/<slug>/preview-image")
    def preview_image(slug):
        project = get_project(slug)
        if not (project / "build/preview.png").is_file():
            abort(404)
        return send_from_directory(project / "build", "preview.png", as_attachment=False)

    @app.post("/projects/<slug>/open-output-folder")
    def open_output_folder(slug):
        project = get_project(slug)
        output_folder = project / "deliverables"
        try:
            subprocess.Popen(["open", str(output_folder)], start_new_session=True)
        except OSError as exc:
            flash(f"Could not open the output folder: {exc}", "error")
        return redirect(url_for("preview_project", slug=slug))

    @app.post("/projects/<slug>/open-project-folder")
    def open_project_folder(slug):
        project = get_project(slug)
        try:
            subprocess.Popen(["open", str(project)], start_new_session=True)
        except OSError as exc:
            flash(f"Could not open the project folder: {exc}", "error")
        return redirect(url_for("dashboard"))

    @app.errorhandler(413)
    def upload_too_large(_error):
        return render_template("new_project.html", error="The GPX file must be 10 MB or smaller."), 413

    return app
