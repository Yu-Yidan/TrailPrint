"""Local browser interface for TrailPrint."""

import subprocess
from dataclasses import asdict, replace
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .bambu import BambuSettings, default_filament_mapping, discover_environment
from .bambu_jobs import BambuJobManager
from .blender_bridge import EXTRA_LABELS, SHAPE_EXTRAS, SHAPE_LABELS, BuildSettings
from .build_settings import current_model_settings
from .filament_palette import import_palette as import_filament_palette
from .filament_palette import load_palette as load_filament_palette
from .filament_palette import palette_revision, role_defaults
from .fonts import MODEL_FONT_BROWSER_CSS
from .gpx import (
    AUTO_RELIEF_LIMIT_MM,
    estimate_print_relief_mm,
)
from .identity import ProjectIdentity, disable_legacy_scheduler
from .jobs import BuildBusy, BuildManager
from .mesh_validation import mesh_role, read_report
from .naming import canonical_3mf_name, human_name, read_project_meta
from .osm_colors import load_palette
from .project_operations import OPERATIONS
from .projects import (
    FUNNEL_STAGES,
    MAX_GPX_BYTES,
    list_deleted_projects,
    list_projects,
    permanently_delete_projects,
    purge_trash,
    read_gpx_sources,
    read_upload,
    resolve_project,
    restore_project,
    trash_project,
    update_production_status,
)
from .terrain_colors import (
    display_elevation,
    elevation_metadata,
    elevation_to_meters,
    load_terrain_defaults,
    save_terrain_preferences,
)
from .title import TITLE_FIT_TOLERANCE_MM, TITLE_LIMITS_MM, TITLE_WIDTH_SCALE, validate_title_fit


def create_app(repo_root: Path = None, build_manager: BuildManager = None,
               bambu_manager: BambuJobManager = None) -> Flask:
    root = (repo_root or Path.cwd()).resolve()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="trailprint-local-only",
        MAX_CONTENT_LENGTH=10 * 1024 * 1024 + 64 * 1024,
        REPO_ROOT=root,
        PRINTS_DIR=root / "prints",
        TRASH_DIR=root / ".trailprint-trash",
    )
    app.extensions["trailprint_builds"] = build_manager or BuildManager()
    app.extensions["trailprint_bambu"] = bambu_manager or BambuJobManager()
    identity = ProjectIdentity(root)
    disable_legacy_scheduler(root)
    purge_trash(app.config["TRASH_DIR"], retention_days=30)

    @app.context_processor
    def local_identity():
        return {"creator_prefix": identity.read().get("prefix", "")}

    @app.route("/settings/identity", methods=["GET", "POST"])
    def identity_settings():
        error = None
        if request.method == "POST":
            try:
                identity.configure(request.form.get("prefix", ""))
            except ValueError as exc:
                error = str(exc)
            else:
                return redirect(url_for("dashboard"))
        return render_template("identity.html", error=error), 400 if error else 200

    @app.get("/")
    def dashboard():
        include_tests = request.args.get("include_tests") == "1"
        projects = [p for p in list_projects(app.config["PRINTS_DIR"])
                    if include_tests or not p.is_test]
        grouped = {key: [] for key, _label in FUNNEL_STAGES}
        for project in projects:
            grouped[project.stage].append(project)
        default_stage = next((key for key, _label in FUNNEL_STAGES if grouped[key]), "created")
        return render_template(
            "dashboard.html", projects=projects, grouped_projects=grouped,
            funnel_stages=FUNNEL_STAGES, default_stage=default_stage,
            deleted_count=len(list_deleted_projects(app.config["TRASH_DIR"])),
            include_tests=include_tests, not_printed=sum(p.stage != "completed" for p in projects))

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
                request.form.get("confirmation", ""))
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Permanently deleted {len(removed)} project(s). This cannot be undone.", "success")
        return redirect(url_for("deleted_projects"))

    @app.get("/projects/new")
    def new_project():
        if not identity.read().get("prefix"):
            return redirect(url_for("identity_settings"))
        return render_template("new_project.html")

    @app.post("/projects")
    def save_project():
        name = request.form.get("name", "").strip()
        uploads = request.files.getlist("gpx")
        try:
            if not uploads:
                raise ValueError("Choose at least one GPX file.")
            sources = []
            for upload in uploads:
                filename = upload.filename or ""
                try:
                    data = read_upload(upload.stream, filename)
                except ValueError as exc:
                    raise ValueError(f"{filename or 'GPX upload'}: {exc}") from exc
                sources.append((filename, data))
            if sum(len(data) for _filename, data in sources) > MAX_GPX_BYTES:
                raise ValueError("Combined GPX uploads must be 10 MB or smaller.")
            if not identity.read().get("prefix"):
                raise ValueError("Set your local project prefix before creating a project.")
            project = identity.create_project(app.config["PRINTS_DIR"], name, sources,
                                              is_test=request.form.get("is_test") == "on")
        except (OSError, ValueError) as exc:
            return render_template(
                "new_project.html", error=str(exc), name=name,
                is_test=request.form.get("is_test") == "on"), 400
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
        deliverables = project / "deliverables"
        job = app.extensions["trailprint_builds"].for_project(project)
        expected_relief = estimate_print_relief_mm(project / "route.gpx")
        defaults = BuildSettings(shape_extra="OUTER TEXT", plate_title=human_name(project))
        restored = current_model_settings(project, defaults)
        # App controls always use the standard recess and internal unit defaults.
        restored = (replace(restored, plate_recess_depth=defaults.plate_recess_depth,
                            unit_system=defaults.unit_system) if restored else None)
        form_settings = BuildSettings.validated(**asdict(restored or defaults))
        preview_path = project / "build/preview.png"
        return render_template(
            "project.html", project=project,
            project_title=human_name(project),
            is_test=bool(read_project_meta(project).get("dim_is_test", False)),
            has_gpx=(project / "route.gpx").is_file(),
            gpx_sources=read_gpx_sources(project),
            stl_files=sorted(path.name for path in deliverables.glob("*.stl")) if deliverables.is_dir() else [],
            shapes=SHAPE_LABELS, extras=SHAPE_EXTRAS, extra_labels=EXTRA_LABELS,
            job=job,
            expected_relief=expected_relief,
            form_settings=form_settings, settings_restored=restored is not None,
            relief_limit=AUTO_RELIEF_LIMIT_MM,
            plate_title=form_settings.plate_title, title_limits=TITLE_LIMITS_MM, title_fit_tolerance=TITLE_FIT_TOLERANCE_MM,
            title_width_scale=TITLE_WIDTH_SCALE, model_font_css=MODEL_FONT_BROWSER_CSS,
            has_preview=preview_path.is_file(),
            validation=read_report(project / "build/validation.json"))

    @app.post("/projects/<slug>/build")
    def build_project(slug):
        project = get_project(slug)
        try:
            settings = BuildSettings.validated(
                shape=request.form.get("shape", "HEXAGON"),
                shape_extra=request.form.get("shape_extra", "OUTER TEXT"),
                resolution=request.form.get("resolution", 8),
                plate_title=request.form.get("plate_title", human_name(project)),
                plate_text_1=request.form.get("plate_text_1", ""),
                plate_text_2=request.form.get("plate_text_2", ""),
                plate_text_3=request.form.get("plate_text_3", ""),
                plate_text_4=request.form.get("plate_text_4", ""),
                plate_text_5=request.form.get("plate_text_5", ""),
                x_offset=request.form.get("x_offset", 0),
                y_offset=request.form.get("y_offset", 0),
                max_terrain_relief_mm=AUTO_RELIEF_LIMIT_MM,
                terrain_elevation_multiplier=request.form.get("terrain_elevation_multiplier", 1),
                dot_magnet_recess=(
                    True if "dot_magnet_recess" not in request.form
                    else "on" in request.form.getlist("dot_magnet_recess")))
            validate_title_fit(settings.plate_title, settings.shape, settings.shape_extra)
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
            with OPERATIONS.hold(project, "deleting"):
                if app.extensions["trailprint_builds"].is_active(project):
                    raise ValueError("This project is currently building. Wait for the build to finish before deleting it.")
                if app.extensions["trailprint_bambu"].is_active(project):
                    raise ValueError("This project is currently exporting. Wait for the Bambu project to finish before deleting it.")
                trash_project(app.config["PRINTS_DIR"], app.config["TRASH_DIR"], slug,
                                        request.form.get("confirmation", ""))
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Moved '{slug}' to local trash. It can be recovered for 30 days.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/projects/<slug>/production-status")
    def production_status(slug):
        project = get_project(slug)
        try:
            update_production_status(project, request.form.get("action", ""))
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
                or Path(filename).suffix.lower() not in {".stl", ".3mf", ".png"}):
            abort(404)
        return send_from_directory(project / "deliverables", filename, as_attachment=True)

    @app.get("/projects/<slug>/preview")
    def preview_project(slug):
        project = get_project(slug)
        deliverables = project / "deliverables"
        files = sorted(deliverables.glob("*.stl")) if deliverables.is_dir() else []
        if not files or not (project / "build/preview.png").is_file():
            flash("Build the project before opening its preview.", "error")
            return redirect(url_for("project_detail", slug=slug))
        preview_files = [{
            "name": path.name, "role": mesh_role(path, slug),
        } for path in files]
        available_roles = {item["role"] for item in preview_files}
        missing_roles = [role for role in
                         ("trail", "terrain", "plate", "text")
                         if role not in available_roles]
        try:
            bambu_environment = load_filament_palette(root, discover_environment)
            bambu_error = ""
            filament_defaults = default_filament_mapping(bambu_environment.filaments)
            terrain_defaults = load_terrain_defaults(
                root, project, bambu_environment.filaments)
            filament_defaults.update(terrain_defaults)
            filament_defaults = role_defaults(root, project, bambu_environment, filament_defaults)
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
        validation = read_report(project / "build/validation.json")
        customer_preview = str(
            validation.get("preview", {}).get("customer_filename", ""))
        if (Path(customer_preview).name != customer_preview
                or not customer_preview.endswith("-preview.png")
                or not (deliverables / customer_preview).is_file()):
            customer_preview = ""
        bambu_job = app.extensions["trailprint_bambu"].for_project(project)
        bambu_validation = read_report(project / "build/bambu-validation.json")
        return render_template(
            "preview.html", project=project, files=preview_files,
            project_title=human_name(project),
            validation=validation,
            customer_preview=customer_preview,
            bambu_environment=bambu_environment, bambu_defaults=filament_defaults,
            palette_revision=palette_revision(bambu_environment) if bambu_environment else "",
            bambu_error=bambu_error, bambu_job=bambu_job,
            bambu_validation=bambu_validation,
            elevation=elevation_view,
            osm_palette=osm_palette,
            bambu_missing_roles=missing_roles,
            bambu_ready=not missing_roles and bool(elevation),
            bambu_output=(project / "deliverables" / canonical_3mf_name(project)).is_file())

    @app.post("/projects/<slug>/import-filaments")
    def import_bambu_filaments(slug):
        get_project(slug)
        try:
            environment = import_filament_palette(root, discover_environment)
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Imported {len(environment.filaments)} project filaments. Review the color assignments.",
                  "success")
        return redirect(url_for("preview_project", slug=slug))

    @app.post("/projects/<slug>/bambu")
    def build_bambu_project(slug):
        project = get_project(slug)
        try:
            environment = load_filament_palette(root, discover_environment)
            if request.form.get("palette_revision") != palette_revision(environment):
                raise ValueError("The filament palette changed. Refresh and review the color selections.")
            elevation = elevation_metadata(project)
            if not elevation:
                raise ValueError("Rebuild the terrain before creating elevation colors.")
            unit_system = str(elevation.get("unit_system", "IMPERIAL")).upper()
            grey_start_m = elevation_to_meters(
                request.form.get("grey_start"), unit_system)
            white_start_m = elevation_to_meters(
                request.form.get("white_start"), unit_system)
            terrain_mode = request.form.get("terrain_mode", "manual")
            active_roles = ["trail", "plate", "text"] + (
                ["black", "green", "grey", "white"] if terrain_mode == "osm"
                else ["land", "mountain", "summit"])
            if any(mesh_role(path, project.name) == "water"
                   for path in (project / "deliverables").glob("*.stl")):
                active_roles.append("water")
            if any(not request.form.get(f"{role}_filament") for role in active_roles):
                raise ValueError("Choose a filament for each active model part before exporting.")
            palette = load_palette(project) if terrain_mode == "osm" else {}
            settings = BambuSettings.validated(
                len(environment.filaments),
                trail=request.form.get("trail_filament"),
                land=request.form.get("land_filament") or 1,
                mountain=request.form.get("mountain_filament") or 1,
                summit=request.form.get("summit_filament") or 1,
                plate=request.form.get("plate_filament"),
                text=request.form.get("text_filament"),
                water=request.form.get("water_filament", 1),
                grey_start_m=grey_start_m, white_start_m=white_start_m,
                black=request.form.get("black_filament") or 1,
                green=request.form.get(
                    "green_filament", request.form.get("land_filament", 1)) or 1,
                grey=request.form.get(
                    "grey_filament", request.form.get("mountain_filament", 1)) or 1,
                white=request.form.get(
                    "white_filament", request.form.get("summit_filament", 1)) or 1,
                terrain_mode=terrain_mode,
                automatic_bands=palette.get("bands", ()),
                generate_diy_kit=request.form.get("generate_diy_kit") == "on")
            color_preferences = {
                **settings.terrain_filaments(),
                "trail": settings.trail_filament, "plate": settings.plate_filament,
                "text": settings.text_filament, "palette_revision": palette_revision(environment),
                "grey_start_m": grey_start_m,
                "white_start_m": white_start_m,
            }
            if any(mesh_role(path, project.name) == "water"
                   for path in (project / "deliverables").glob("*.stl")):
                color_preferences["water"] = settings.water_filament
            save_terrain_preferences(
                root, project, color_preferences,
                save_defaults=request.form.get("save_terrain_defaults") == "on")
            job = app.extensions["trailprint_bambu"].start(project, settings, environment=environment)
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
        return render_template("new_project.html", error="Combined GPX uploads must be 10 MB or smaller."), 413

    return app
