"""Local browser interface for TrailPrint."""

import json
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

from .bambu import (
    BambuSettings,
    default_filament_mapping,
    discover_environment,
    nominal_layer_height,
)
from .bambu_jobs import BambuJobManager
from .blender_bridge import EXTRA_LABELS, SHAPE_EXTRAS, SHAPE_LABELS, BuildSettings
from .build_settings import current_model_settings
from .filament_palette import (
    OBJECT_DEFAULTS,
    ensure_terrain_palette,
    palette_revision,
    role_defaults,
)
from .filament_palette import load_object_palette as load_filament_palette
from .filament_palette import restore_object_palette as import_filament_palette
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
    elevation_metadata,
    load_terrain_defaults,
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
        preview_files = [
            {
                "name": path.name,
                "role": mesh_role(path, slug),
            }
            for path in files
        ]
        available_roles = {item["role"] for item in preview_files}
        missing_roles = [
            role for role in ("trail", "terrain", "plate", "text") if role not in available_roles
        ]
        try:
            bambu_environment, terrain_slots = ensure_terrain_palette(
                root, load_filament_palette(root, discover_environment)
            )
            bambu_error = ""
            filament_defaults = default_filament_mapping(bambu_environment.filaments)
            terrain_defaults = load_terrain_defaults(root, project, bambu_environment.filaments)
            filament_defaults.update(terrain_defaults)
            filament_defaults = role_defaults(root, project, bambu_environment, filament_defaults)
        except ValueError as exc:
            bambu_environment, filament_defaults, terrain_defaults, bambu_error = None, {}, {}, str(exc)
        from .terrain_bands import COLORS
        from .terrain_colors import _read_json

        saved_bands = _read_json(project / "terrain-bands.json")
        if bambu_environment:
            filament_defaults["terrain"] = terrain_slots.get(
                saved_bands.get("terrain", "green"), terrain_slots["green"]
            )
        elevation = elevation_metadata(project)
        osm_palette = load_palette(project)
        validation = read_report(project / "build/validation.json")
        customer_preview = str(validation.get("preview", {}).get("customer_filename", ""))
        if (
            Path(customer_preview).name != customer_preview
            or not customer_preview.endswith("-preview.png")
            or not (deliverables / customer_preview).is_file()
        ):
            customer_preview = ""
        bambu_job = app.extensions["trailprint_bambu"].for_project(project)
        return render_template(
            "preview.html",
            project=project,
            files=preview_files,
            project_title=human_name(project),
            validation=validation,
            customer_preview=customer_preview,
            preview_version=(project / "build/preview.png").stat().st_mtime_ns,
            has_side_preview=(project / "build/preview-side.png").is_file(),
            bambu_environment=bambu_environment,
            bambu_defaults=filament_defaults,
            palette_revision=palette_revision(bambu_environment) if bambu_environment else "",
            bambu_error=bambu_error,
            bambu_job=bambu_job,
            band_editor=dict(
                elevation=elevation,
                colors=COLORS,
                saved=saved_bands,
                suggestions=osm_palette.get("bands", []) if osm_palette else [],
                layer_height=nominal_layer_height(bambu_environment) if bambu_environment else 0.2,
            ),
            bambu_missing_roles=missing_roles,
            bambu_ready=not missing_roles,
            bambu_output=(project / "deliverables" / canonical_3mf_name(project)).is_file(),
        )

    @app.post("/projects/<slug>/import-filaments")
    def import_bambu_filaments(slug):
        project = get_project(slug)
        try:
            import_filament_palette(root, discover_environment, project)
            from .terrain_colors import _atomic_json

            _atomic_json(
                project / "terrain-bands.json", dict(terrain="green", advanced=False, bands=[])
            )
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Restored the default object colors. Review the assignments.", "success")
        return redirect(url_for("preview_project", slug=slug))

    from .preview_jobs import PreviewJobs
    preview_jobs = PreviewJobs()
    app.extensions['trailprint_previews'] = preview_jobs

    def read_bambu_submission(project):
        from .terrain_bands import validate_ranges

        environment, slots = ensure_terrain_palette(
            root, load_filament_palette(root, discover_environment)
        )
        if request.form.get("palette_revision") != palette_revision(environment):
            raise ValueError("The filament palette changed. Refresh and review the color selections.")
        mode = "custom" if request.form.get("advanced_terrain") == "on" else "single"
        color = request.form.get("terrain_color", "green")
        if color not in slots:
            raise ValueError("Choose a valid terrain color.")
        ranges = json.loads(request.form.get("terrain_bands", "[]")) if mode == "custom" else []
        if mode == "custom":
            ranges = validate_ranges(
                ranges, elevation_metadata(project), nominal_layer_height(environment)
            )
        roles = ["trail", "plate", "text"]
        settings = BambuSettings.validated(
            len(environment.filaments),
            **{role: request.form.get(role + "_filament") for role in roles},
            land=slots[color],
            mountain=OBJECT_DEFAULTS["attribution"],
            water=request.form.get("water_filament") or OBJECT_DEFAULTS["water"],
            terrain_mode=mode,
            custom_bands=ranges,
            band_filaments=[slots[item["color"]] for item in ranges],
        )
        return settings, environment, color, mode, roles

    def save_bambu_preferences(project, settings, environment, color, mode, roles):
        from .terrain_colors import _atomic_json, _read_json, project_settings_path

        _atomic_json(
            project / "terrain-bands.json",
            dict(
                terrain=color,
                advanced=mode == "custom",
                bands=json.loads(request.form.get("terrain_bands", "[]")),
            ),
        )

        preferences = _read_json(project_settings_path(project))
        preferences.update({role: getattr(settings, role + "_filament") for role in roles})
        preferences.update(
            water=settings.water_filament, palette_revision=palette_revision(environment)
        )
        _atomic_json(project_settings_path(project), preferences)

    @app.post('/projects/<slug>/refresh-preview')
    def refresh_project_preview(slug):
        project = get_project(slug)
        try:
            submission = read_bambu_submission(project)
            settings, environment, *_ = submission
            job = preview_jobs.start(
                project, settings, environment, lambda: save_bambu_preferences(project, *submission)
            )
            return jsonify(id=job["id"], state=job["state"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/projects/<slug>/bambu')
    def build_bambu_project(slug):
        project = get_project(slug)
        try:
            submission = read_bambu_submission(project)
            settings, environment, *_ = submission
            save_bambu_preferences(project, *submission)
            job = app.extensions["trailprint_bambu"].start(project, settings, environment=environment)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            flash(str(exc), "error")
        else:
            flash("Bambu Studio project export started.", "success")
        return redirect(
            url_for("preview_project", slug=slug, bambu_job=job.id if "job" in locals() else None)
        )

    @app.get('/projects/<slug>/preview-jobs/<job_id>')
    def preview_job_status(slug, job_id):
        project = get_project(slug)
        job = preview_jobs.jobs.get(job_id)
        if not job or job["project"] != str(project.resolve()):
            abort(404)
        return jsonify({key: value for key, value in job.copy().items() if key != "project"})

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
        filename = "preview-side.png" if request.args.get("view") == "side" else "preview.png"
        if not (project / "build" / filename).is_file():
            abort(404)
        return send_from_directory(project / "build", filename, as_attachment=False)

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
