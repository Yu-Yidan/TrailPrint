"""Cached reverse geocoding for local TrailPrint projects."""

import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from .gpx import read_gpx


LOCATION_FILE = "location.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "TrailPrint/0.1 (https://github.com/Yu-Yidan/TrailPrint)"
EMPTY_LOCATION = {"city": "", "state": "", "country": ""}
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


def read_location(project: Path) -> Dict[str, str]:
    path = project / LOCATION_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        location = data.get("location", {})
        if not isinstance(location, dict):
            return dict(EMPTY_LOCATION)
        return {key: str(location.get(key, "")) for key in EMPTY_LOCATION}
    except (OSError, ValueError, TypeError):
        return dict(EMPTY_LOCATION)


def has_location_cache(project: Path) -> bool:
    try:
        data = json.loads((project / LOCATION_FILE).read_text(encoding="utf-8"))
        return isinstance(data.get("location"), dict)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _write_metadata(project: Path, location: Dict[str, str], latitude=None, longitude=None,
                    source="user") -> None:
    payload = {
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            **{key: str(location.get(key, "")) for key in EMPTY_LOCATION},
            "source": source,
            "looked_up_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix=".location-", suffix=".json", dir=str(project))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, project / LOCATION_FILE)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def save_location(project: Path, city: str, state: str, country: str) -> Dict[str, str]:
    location = {"city": city.strip(), "state": state.strip(), "country": country.strip()}
    if any(len(value) > 200 for value in location.values()):
        raise ValueError("Location fields must be 200 characters or fewer.")
    _write_metadata(project, location)
    return location


def _request_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def detect_location(project: Path, timeout: float = 5.0) -> Dict[str, str]:
    """Reverse-geocode the first GPX point and persist the normalized result."""
    global _LAST_REQUEST
    latitude, longitude, _elevation = read_gpx(project / "route.gpx")[0]
    query = urllib.parse.urlencode({
        "format": "jsonv2", "lat": latitude, "lon": longitude,
        "addressdetails": 1, "accept-language": "en",
    })
    with _REQUEST_LOCK:
        delay = 1.0 - (time.monotonic() - _LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        try:
            result = _request_json(f"{NOMINATIM_URL}?{query}", timeout)
        finally:
            _LAST_REQUEST = time.monotonic()
    address = result.get("address", {})
    if not isinstance(address, dict):
        raise ValueError("Location service returned an invalid address.")
    city = next((address.get(key) for key in
                 ("city", "town", "village", "municipality", "hamlet", "county")
                 if address.get(key)), "")
    state = next((address.get(key) for key in ("state", "region", "province", "state_district")
                  if address.get(key)), "")
    location = {"city": str(city), "state": str(state), "country": str(address.get("country", ""))}
    _write_metadata(project, location, latitude, longitude, "OpenStreetMap Nominatim")
    return location
