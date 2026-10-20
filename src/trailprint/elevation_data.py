"""Coverage-checked Mapterhorn sampling, independent of Blender and legacy caches."""

import hashlib
import json
import math
from pathlib import Path

TILE_SIZE = 512
CACHE_VERSION = 1


class ElevationCoverageError(ValueError):
    pass


def tile_position(lat, lon, zoom):
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    radians = math.radians(lat)
    y = (1 - math.asinh(math.tan(radians)) / math.pi) / 2 * n
    return int(x), int(y), min(511, int(x % 1 * 512)), min(511, int(y % 1 * 512))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def sample_elevations(coords, zoom, cache_dir, legacy_tiles, fetch, decode,
                      report_path, disable_cache=False, progress=None):
    """Read every required tile; never turn an unavailable sample into a height.

    fetch(z, x, y) returns bytes; decode(path) returns top-down RGBA rows.
    Only a 404 permits the provider's documented parent fallback down to z12.
    Legacy compiled elevation arrays are deliberately never consulted.
    """
    cache_dir, legacy_tiles, report_path = map(Path, (cache_dir, legacy_tiles, report_path))
    report = {"valid": False, "provider": "MAPTERHORN", "version": CACHE_VERSION,
              "required_samples": len(coords), "verified_samples": 0, "tiles": [],
              "errors": [], "legacy_result_cache_used": False}
    _write(report_path, report)
    try:
        if not coords:
            raise ElevationCoverageError("No elevation coordinates were generated.")
        groups = {}
        for index, (lat, lon) in enumerate(coords):
            if not (math.isfinite(lat) and math.isfinite(lon)
                    and -85.051129 < lat < 85.051129 and -180 <= lon < 180):
                raise ElevationCoverageError(f"Invalid elevation coordinate: {lat}, {lon}")
            x, y, _, _ = tile_position(lat, lon, zoom)
            groups.setdefault((zoom, x, y), []).append(index)
        values = [None] * len(coords)
        loaded = {}

        def load(z, x, y):
            key = (z, x, y)
            if key in loaded:
                return loaded[key]
            filename = f"mapterhorn_{z}_{x}_{y}.webp"
            destination = cache_dir / "tiles" / filename
            causes = []
            if not disable_cache:
                for path in (destination, legacy_tiles / filename):
                    if not path.exists():
                        continue
                    try:
                        data = path.read_bytes()
                        pixels = decode(path)
                        if len(pixels) != TILE_SIZE or any(len(row) != TILE_SIZE for row in pixels):
                            raise ValueError("expected a 512 by 512 tile")
                        loaded[key] = (pixels, _digest(data))
                        return loaded[key]
                    except Exception as exc:
                        causes.append(f"cached tile: {exc}")
            for attempt in range(3):
                try:
                    data = fetch(z, x, y)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_suffix(".download.webp")
                    temporary.write_bytes(data)
                    try:
                        pixels = decode(temporary)
                        if len(pixels) != TILE_SIZE or any(len(row) != TILE_SIZE for row in pixels):
                            raise ValueError("expected a 512 by 512 tile")
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                    loaded[key] = (pixels, _digest(data))
                    return loaded[key]
                except Exception as exc:
                    causes.append(f"attempt {attempt + 1}: {exc}")
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status == 404 and z > 12:
                        return load(z - 1, x // 2, y // 2)
                    if status == 404:
                        break
            raise ElevationCoverageError(
                f"Missing or unreadable Mapterhorn tile {z}/{x}/{y}: " + "; ".join(causes))

        for group_number, ((z, x, y), indices) in enumerate(groups.items(), 1):
            # Keep memory bounded on routes spanning hundreds of DEM tiles.
            loaded.clear()
            pixels, digest = load(z, x, y)
            # A parent tile has the same array shape. Resolve its actual zoom
            # from the successfully loaded entry rather than sampling child XY.
            actual = next(key for key, value in loaded.items() if value[0] is pixels)
            for index in indices:
                lat, lon = coords[index]
                _, _, px, py = tile_position(lat, lon, actual[0])
                pixel = pixels[py][px]
                if (len(pixel) != 4 or not all(math.isfinite(c) for c in pixel)
                        or pixel[3] != 255 or any(c < 0 or c > 255 for c in pixel[:3])):
                    raise ElevationCoverageError(
                        f"Invalid elevation pixel in tile {actual[0]}/{actual[1]}/{actual[2]}"
                        f" at ({px}, {py})")
                r, g, b = pixel[:3]
                height = r * 256 + g + b / 256 - 32768
                if not -12000 <= height <= 9000:
                    raise ElevationCoverageError(f"No-data elevation in tile {actual}: {height}")
                values[index] = height
            report["tiles"].append({"requested": [z, x, y], "source": list(actual),
                                    "sha256": digest, "samples": len(indices)})
            report["verified_samples"] += len(indices)
            if progress:
                progress(int(group_number / len(groups) * 100))
        # Store only arrays with proven full coverage and a content fingerprint.
        fingerprint = _digest(json.dumps([CACHE_VERSION, coords, report["tiles"]]).encode())
        result_path = cache_dir / "results" / (fingerprint + ".json")
        payload = json.dumps(values)
        cached = None
        if not disable_cache and result_path.exists():
            try:
                cached = json.loads(result_path.read_text())
            except (OSError, ValueError):
                pass
        report["result_cache_hit"] = bool(
            cached and cached.get("fingerprint") == fingerprint
            and cached.get("sha256") == _digest(payload.encode())
            and cached.get("values") == values)
        if not report["result_cache_hit"]:
            _write(result_path, {"fingerprint": fingerprint, "sha256": _digest(payload.encode()),
                                 "values": values, "coverage": report["tiles"]})
        report["valid"] = True
        _write(report_path, report)
        return values
    except Exception as exc:
        report["errors"].append(str(exc))
        _write(report_path, report)
        raise ElevationCoverageError(f"Elevation coverage validation failed: {exc}") from exc


def verified_blender_elevations(obj, props, elevation_utils, geo_utils, cache_dir,
                                report_path, decode, fetch, progress=None):
    world, subdivisions, disabled, min_lat, max_lat, min_lon, max_lon = (
        elevation_utils.compute_and_store_tile_bounds(obj))
    distances = [geo_utils.haversine(lat, min_lon, lat, max_lon) * 1000
                 for lat in (min_lat, max_lat)]
    spacing = max(distances) / (1 + 2 ** (subdivisions + 1))
    zoom = min(15, 2 + max(0, math.ceil(math.log2(156543 / max(spacing, 1e-9)))))
    coords = [geo_utils.convert_to_geo(v.x, v.y) for v in world]
    values = sample_elevations(coords, zoom, cache_dir, elevation_utils.const.terrarium_cache_dir,
                               fetch, decode, report_path, disabled, progress)
    props["o_verticesMap"] = str(len(values))
    props.lowestElevation = min(values)
    props.highestElevation = max(values)
    props.sAdditionalExtrusion = min(values)
    return values, max(values) - min(values)
