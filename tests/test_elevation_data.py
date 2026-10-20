import json
from types import SimpleNamespace

import pytest

from trailprint.elevation_data import ElevationCoverageError, sample_elevations


def pixels(height=0, alpha=255):
    encoded = height + 32768
    pixel = (int(encoded // 256), int(encoded % 256), 0, alpha)
    return [[pixel] * 512] * 512


def sample(tmp_path, fetch, decode=lambda path: pixels(), coords=None, zoom=12):
    return sample_elevations(coords or [(35.7, -93.73)], zoom, tmp_path / "cache",
                             tmp_path / "legacy", fetch, decode, tmp_path / "report.json")


def test_zero_and_negative_elevations_are_valid(tmp_path):
    for height in (0, -20, 600):
        assert sample(tmp_path, lambda *args: b"tile", lambda path: pixels(height)) == [height]
        assert json.loads((tmp_path / "report.json").read_text())["valid"]


def test_missing_tile_retries_and_blocks(tmp_path):
    calls = []

    def fetch(*tile):
        calls.append(tile)
        raise OSError("network unavailable")

    with pytest.raises(ElevationCoverageError, match="12/981/1612.*network unavailable"):
        sample(tmp_path, fetch)
    assert len(calls) == 3
    report = json.loads((tmp_path / "report.json").read_text())
    assert not report["valid"] and report["verified_samples"] == 0
    assert not list((tmp_path / "cache").glob("results/*"))


def test_retry_recovers_and_corrupt_cached_tile_is_refetched(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "mapterhorn_12_981_1612.webp").write_bytes(b"corrupt")
    calls = []

    def fetch(*tile):
        calls.append(tile)
        if len(calls) == 1:
            raise OSError("transient")
        return b"good"

    def decode(path):
        if path.read_bytes() != b"good":
            raise ValueError("corrupt image")
        return pixels(100)

    assert sample(tmp_path, fetch, decode) == [100]
    assert len(calls) == 2
    assert sample(tmp_path, lambda *args: pytest.fail("unexpected fetch"), decode) == [100]
    assert json.loads((tmp_path / "report.json").read_text())["result_cache_hit"]


def test_documented_404_parent_fallback(tmp_path):
    calls = []

    def fetch(z, x, y):
        calls.append(z)
        if z > 12:
            error = OSError("404")
            error.response = SimpleNamespace(status_code=404)
            raise error
        return b"tile"

    assert sample(tmp_path, fetch, zoom=14) == [0]
    assert calls == [14, 13, 12]
    tile = json.loads((tmp_path / "report.json").read_text())["tiles"][0]
    assert tile["source"] == [12, 981, 1612]


def test_missing_global_tile_cannot_fall_back_further(tmp_path):
    calls = []

    def fetch(z, x, y):
        calls.append(z)
        error = OSError("404 not found")
        error.response = SimpleNamespace(status_code=404)
        raise error

    with pytest.raises(ElevationCoverageError, match="12/981/1612.*404"):
        sample(tmp_path, fetch, zoom=13)
    assert calls == [13, 12]


def test_parent_fallback_samples_parent_pixel_coordinates(tmp_path):
    from trailprint.elevation_data import tile_position

    def fetch(z, x, y):
        if z > 12:
            error = OSError("404")
            error.response = SimpleNamespace(status_code=404)
            raise error
        return b"tile"

    parent_pixels = [list(row) for row in pixels()]
    _, _, px, py = tile_position(35.7, -93.73, 12)
    parent_pixels[py][px] = (128, 123, 0, 255)
    assert sample(tmp_path, fetch, lambda path: parent_pixels, zoom=14) == [123]


@pytest.mark.parametrize("decoded", [[], pixels(0, 0), pixels(-32768)])
def test_corrupt_partial_and_no_data_tiles_block(tmp_path, decoded):
    with pytest.raises(ElevationCoverageError):
        sample(tmp_path, lambda *args: b"tile", lambda path: decoded)
    assert not json.loads((tmp_path / "report.json").read_text())["valid"]


def test_legacy_and_tampered_result_arrays_are_never_trusted(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "old.elev").write_bytes(b"unverified zeros")
    assert sample(tmp_path, lambda *args: b"tile", lambda path: pixels(123)) == [123]
    result = next((tmp_path / "cache/results").glob("*.json"))
    cache = json.loads(result.read_text())
    cache["values"] = [0]
    result.write_text(json.dumps(cache))
    assert sample(tmp_path, lambda *args: b"tile", lambda path: pixels(123)) == [123]
    assert not json.loads((tmp_path / "report.json").read_text())["result_cache_hit"]


def test_partial_coverage_blocks_entire_build(tmp_path):
    def fetch(z, x, y):
        if x != 981:
            raise OSError("missing second tile")
        return b"tile"

    with pytest.raises(ElevationCoverageError, match="missing second tile"):
        sample(tmp_path, fetch, coords=[(35.7, -93.73), (35.7, -92.0)])
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["verified_samples"] == 1
    assert not report["valid"]
