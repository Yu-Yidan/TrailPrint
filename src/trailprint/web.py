"""Local browser interface for TrailPrint."""

from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, url_for

from .blender_bridge import BuildSettings, EXTRA_LABELS, SHAPE_EXTRAS, SHAPE_LABELS
from .config import load_config
from .jobs import BuildBusy, BuildManager
from .projects import create_project, list_projects, purge_trash, read_upload, resolve_project, trash_project


def create_app(repo_root: Path = None, build_manager: BuildManager = None) -> Flask:
    root = (repo_root or Path.cwd()).resolve()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="trailprint-local-only",
        MAX_CONTENT_LENGTH=10 * 1024 * 1024 + 64 * 1024,
        REPO_ROOT=root,
        PRINTS_DIR=root / "prints",
        PRESETS_DIR=root / "presets",
        TRASH_DIR=root / ".trailprint-trash",
    )
    app.extensions["trailprint_builds"] = build_manager or BuildManager()
    purge_trash(app.config["TRASH_DIR"], retention_days=30)

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", projects=list_projects(app.config["PRINTS_DIR"]))

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
            project = create_project(
                app.config["PRINTS_DIR"], app.config["PRESETS_DIR"], name, data)
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
        deliverables = project / "deliverables"
        job = app.extensions["trailprint_builds"].for_project(project)
        return render_template(
            "project.html", project=project,
            has_gpx=(project / "route.gpx").is_file(),
            stl_files=sorted(path.name for path in deliverables.glob("*.stl")) if deliverables.is_dir() else [],
            shapes=SHAPE_LABELS, extras=SHAPE_EXTRAS, extra_labels=EXTRA_LABELS,
            job=job)

    @app.post("/projects/<slug>/build")
    def build_project(slug):
        project = get_project(slug)
        try:
            settings = BuildSettings.validated(
                request.form.get("shape", "HEXAGON"), request.form.get("shape_extra", "NONE"),
                request.form.get("resolution", 4))
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
            trash_project(app.config["PRINTS_DIR"], app.config["TRASH_DIR"], slug,
                          request.form.get("confirmation", ""))
        except (OSError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Moved '{slug}' to local trash. It can be recovered for 30 days.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/builds/<job_id>")
    def build_status(job_id):
        job = app.extensions["trailprint_builds"].get(job_id)
        if job is None:
            abort(404)
        return jsonify(app.extensions["trailprint_builds"].serialize(job))

    @app.get("/projects/<slug>/files/<filename>")
    def download_file(slug, filename):
        project = get_project(slug)
        if Path(filename).name != filename or Path(filename).suffix.lower() != ".stl":
            abort(404)
        return send_from_directory(project / "deliverables", filename, as_attachment=True)

    @app.errorhandler(413)
    def upload_too_large(_error):
        return render_template("new_project.html", error="The GPX file must be 10 MB or smaller."), 413

    return app
