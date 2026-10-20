import json
import shutil
import subprocess
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.datastructures import MultiDict

from trailprint.blender_bridge import BuildSettings
from trailprint.build_settings import current_model_settings
from trailprint.projects import create_project
from trailprint.web import create_app

ROOT = Path(__file__).resolve().parents[1]


class Form(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.ids = {}
        self.selected_shape = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids[attrs["id"]] = attrs
        if tag == "input" and "name" in attrs:
            self.inputs[attrs["name"]] = attrs
        if tag == "option" and "selected" in attrs:
            self.selected_shape = attrs["value"]

    def submission(self):
        values = MultiDict()
        for name, attrs in self.inputs.items():
            if attrs.get("type") != "checkbox" or "checked" in attrs:
                values.add(name, attrs.get("value", "on"))
        # HTML includes a hidden 'off' alongside the magnet checkbox.
        values.add("dot_magnet_recess", "off")
        values.add("shape", self.selected_shape)
        values.add("shape_extra", self.ids["shape-extra"]["data-initial-value"])
        values.add("resolution", self.ids["resolution"]["value"])
        return values


def save_run(project, settings, identifier="current", state="success", point_current=True):
    directory = project / "products/fridge_magnet_raised_map"
    (directory / "runs").mkdir(parents=True, exist_ok=True)
    (directory / "runs" / f"{identifier}.json").write_text(json.dumps({
        "state": state, "settings": settings,
    }))
    if point_current:
        (directory / "current.json").write_text(json.dumps({"id_build": identifier}))


@pytest.fixture
def saved():
    return BuildSettings.validated(
        shape="CIRCLE", shape_extra="OUTER TEXT", resolution=9,
        plate_title="Pedal Fest", plate_text_1="Custom distance", plate_text_2="",
        plate_text_3="Finisher", plate_text_4="", plate_text_5="NC",
        unit_system="METRIC", x_offset=0, y_offset=-2.75,
        plate_recess_depth=0, dot_magnet_recess=False,
        city="Bent Creek", state="North Carolina", country="USA")


@pytest.fixture
def page(tmp_path, saved):
    (tmp_path / "prints").mkdir()
    manager = SimpleNamespace(for_project=lambda _: None,
                              start=Mock(return_value=SimpleNamespace(id="next-build")))
    app = create_app(tmp_path, build_manager=manager)
    app.config["TESTING"] = True
    project = create_project(tmp_path / "prints", "Saved Route",
                             (ROOT / "tests/fixtures/example-route.gpx").read_bytes())
    save_run(project, asdict(saved))
    return app, manager, project


def test_saved_form_survives_restart_and_submits_identical_settings(page, saved):
    app, manager, project = page
    for client in (app.test_client(), create_app(app.config["REPO_ROOT"], manager).test_client()):
        response = client.get(f"/projects/{project.name}")
        assert response.status_code == 200
        form = Form(response.get_data(as_text=True))
        assert form.selected_shape == saved.shape
        assert form.ids["shape-extra"]["data-initial-value"] == saved.shape_extra
        assert form.ids["plate-text-settings"]["data-restored"] == "true"
        assert b"Saved location" in response.data
        assert "checked" in form.inputs["use_metric"]
        assert "checked" not in form.inputs["dot_magnet_recess"]
        result = client.post(f"/projects/{project.name}/build", data=form.submission())
        assert result.status_code == 302
        assert manager.start.call_args.args[1] == saved


@pytest.mark.parametrize("state", ["failed", "cancelled", "running", "queued"])
def test_later_unsuccessful_attempt_does_not_replace_current_settings(page, saved, state):
    app, _, project = page
    save_run(project, {"plate_title": "Unsuccessful edit"}, "later", state, point_current=False)
    form = Form(app.test_client().get(f"/projects/{project.name}").get_data(as_text=True))
    assert form.inputs["plate_title"]["value"] == saved.plate_title
    assert form.inputs["plate_text_2"]["value"] == ""


@pytest.mark.parametrize("bad", [None, [], {"shape": "INVALID"}, {"resolution": 99},
                                 {"plate_title": []}, {"dot_magnet_recess": "false"}])
def test_unusable_saved_settings_fall_back_safely(tmp_path, bad):
    save_run(tmp_path, bad)
    assert current_model_settings(tmp_path, BuildSettings()) is None


def test_missing_corrupt_and_unsafe_current_records_fall_back(tmp_path):
    defaults = BuildSettings()
    assert current_model_settings(tmp_path, defaults) is None
    directory = tmp_path / "products/fridge_magnet_raised_map"
    directory.mkdir(parents=True)
    for value in ('not json', '[]', '{"id_build":"../../other"}', '{"id_build":"missing"}'):
        (directory / "current.json").write_text(value)
        assert current_model_settings(tmp_path, defaults) is None


def test_partial_older_settings_use_compatible_defaults(tmp_path):
    defaults = BuildSettings(shape_extra="OUTER TEXT", plate_title="Default")
    save_run(tmp_path, {"shape": "SQUARE", "plate_title": "Older model"})
    restored = current_model_settings(tmp_path, defaults)
    assert restored.shape == "SQUARE"
    assert restored.shape_extra == "NONE"
    assert restored.resolution == defaults.resolution


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the JS check")
def test_client_initialization_preserves_saved_values_and_unit_edits(page):
    app, _, project = page
    save_run(project, {**asdict(current_model_settings(project, BuildSettings())),
                       "shape_extra": "NONE"})
    form = Form(app.test_client().get(f"/projects/{project.name}").get_data(as_text=True))
    # Exercise the actual project script with a minimal DOM and real rendered inputs.
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const form = JSON.parse(process.argv[1]), elements = new Map();
function element(attrs = {}) {
  return {value: attrs.value || '', checked: Object.hasOwn(attrs, 'checked'),
    disabled: false, dataset: Object.fromEntries(Object.entries(attrs)
      .filter(([key]) => key.startsWith('data-'))
      .map(([key,value]) => [key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),value])),
    listeners: {}, classList: {toggle(){}}, style: {},
    addEventListener(type, action) {this.listeners[type] = action},
    setAttribute(){}, setCustomValidity(){}, querySelectorAll(){return []},
    replaceChildren(...children){this.children = children},
  };
}
for (const [id,attrs] of Object.entries(form.ids)) elements.set('#'+id, element(attrs));
elements.get('#shape').value = form.selected_shape;
elements.set('.build-button', element());
elements.set('.location-status', element());
const document = {querySelector(selector){return elements.get(selector) || null},
  createElement(tag){return tag === 'canvas' ? {getContext(){return {
    measureText(){return {width:1,actualBoundingBoxAscent:1,actualBoundingBoxDescent:0}}
  }}} : element()}};
let fetches = 0;
const window = {TRAILPRINT_EXTRAS:{CIRCLE:['NONE','OUTER TEXT'],SQUARE:['NONE']},
 TRAILPRINT_EXTRA_LABELS:{NONE:'None','OUTER TEXT':'Outer text'},
 TRAILPRINT_TITLE_LIMITS:{}, TRAILPRINT_TRIP_VALUES:{
  METRIC:{length:'0.66 km'}, IMPERIAL:{length:'0.41 mi'}},
 TRAILPRINT_MODEL_FONT_CSS:'bold Arial'};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),
 {document,window,fetch(){fetches++; throw Error('unexpected location lookup')}});
assert.equal(elements.get('#shape-extra').value,'NONE');
assert.equal(elements.get('#resolution-value').textContent,'9');
assert.equal(elements.get('#plate-text-1').value,'Custom distance');
assert.equal(fetches,0);
const metric = elements.get('#use-metric'), distance = elements.get('#plate-text-1');
metric.checked = false; metric.listeners.change();
assert.equal(distance.value,'Custom distance');
distance.value='0.41 mi'; distance.listeners.input();
metric.checked = true; metric.listeners.change();
assert.equal(distance.value,'0.66 km');
const shape = elements.get('#shape'); shape.value='SQUARE'; shape.listeners.change();
assert.equal(elements.get('#shape-extra').value,'NONE');
"""
    payload = json.dumps({"ids": form.ids, "selected_shape": form.selected_shape})
    subprocess.run([shutil.which("node"), "-e", script, payload,
                    str(ROOT / "src/trailprint/static/project.js")], check=True,
                   capture_output=True, text=True)


def test_retired_creek_settings_are_ignored_without_losing_other_settings(page, saved):
    app, manager, project = page
    legacy = {**asdict(saved), "small_creeks_enabled": True,
              "small_creek_ids": ["way/123"], "small_creek_width_mm": 2,
              "small_creek_snapshot": "obsolete"}
    save_run(project, legacy)
    assert current_model_settings(project, BuildSettings()) == saved
    client = app.test_client()
    html = client.get(f"/projects/{project.name}").get_data(as_text=True)
    assert 'creek-settings' not in html and 'creeks.js' not in html and 'creeks.css' not in html
    form = Form(html).submission()
    form.update({"small_creeks_enabled": "on", "small_creek_ids": "invalid legacy input",
                 "small_creek_width_mm": "invalid", "small_creek_snapshot": "missing"})
    assert client.post(f"/projects/{project.name}/build", data=form).status_code == 302
    assert manager.start.call_args.args[1] == saved
    assert not any('creek' in key for key in asdict(manager.start.call_args.args[1]))
    assert client.post(f"/projects/{project.name}/creeks/preview").status_code == 404
