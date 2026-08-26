import base64
import json
import os
import re
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fitdecode

from src.integrations.coros_mcp import call_coros_tool


from src.runtime.paths import ROOT_DIR  # noqa: E402
DEFAULT_FIT_DIR = ROOT_DIR / "data" / "coros-report" / "fit-files"
DEFAULT_ROUTE_MAP_DIR = ROOT_DIR / "data" / "coros-report" / "route-maps"
SEMICIRCLE_TO_DEGREES = 180 / 2**31
MAX_ROUTE_POINTS = 120


@dataclass(frozen=True)
class FitArchiveResult:
    paths: tuple[Path, ...]
    downloaded: bool
    message: str


@dataclass(frozen=True)
class RouteMapResult:
    path: Path | None
    point_count: int
    message: str


def fit_archive_enabled() -> bool:
    return os.getenv("COROS_FIT_ARCHIVE_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def route_map_enabled() -> bool:
    return os.getenv("COROS_ROUTE_MAP_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _base_fit_dir() -> Path:
    return Path(os.getenv("COROS_FIT_ARCHIVE_DIR", str(DEFAULT_FIT_DIR))).expanduser()


def _route_map_dir() -> Path:
    return Path(os.getenv("COROS_ROUTE_MAP_DIR", str(DEFAULT_ROUTE_MAP_DIR))).expanduser()


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _activity_timestamp(activity: dict[str, Any]) -> int:
    for key in ("startTimestamp", "endTimestamp", "timestamp"):
        value = activity.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def _activity_date(activity: dict[str, Any]) -> str:
    value = _first_present(activity, ("date", "startDate", "day"))
    if value:
        return str(value).replace("/", "-")[:10]

    timestamp = _activity_timestamp(activity)
    if timestamp:
        return datetime.fromtimestamp(timestamp, UTC).astimezone().strftime("%Y-%m-%d")
    return "unknown-date"


def _activity_slug(activity: dict[str, Any]) -> str:
    label_id = str(activity.get("labelId") or "unknown-label")
    sport_type = str(activity.get("sportType") or "unknown-sport")
    start = str(activity.get("startTimestamp") or "")
    end = str(activity.get("endTimestamp") or "")
    raw = "-".join(item for item in (_activity_date(activity), label_id, sport_type, start, end) if item)
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-") or "activity"


def _activity_fit_dir(activity: dict[str, Any]) -> Path:
    return _base_fit_dir() / _activity_date(activity) / _activity_slug(activity)


def _existing_fit_paths(activity: dict[str, Any]) -> tuple[Path, ...]:
    folder = _activity_fit_dir(activity)
    if not folder.exists():
        return ()
    return tuple(sorted(folder.glob("*.fit")))


def _detail_args(activity: dict[str, Any]) -> dict[str, Any]:
    label_id = activity.get("labelId")
    sport_type = activity.get("sportType")
    if label_id is None or sport_type is None:
        raise RuntimeError("COROS activity is missing labelId or sportType.")
    return {"labelId": str(label_id), "sportType": int(sport_type), "limit": 1}


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, dict | list):
                items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items


def _resource_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        resource = item.get("resource")
        if isinstance(resource, dict):
            key = (str(resource.get("uri") or ""), str(resource.get("blob") or "")[:80])
            if key not in seen:
                seen.add(key)
                resources.append(resource)
        elif item.get("blob") is not None and item.get("uri") is not None:
            key = (str(item.get("uri") or ""), str(item.get("blob") or "")[:80])
            if key not in seen:
                seen.add(key)
                resources.append(item)
    return resources


def _text_items(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in _walk(payload):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"])
    return texts


def _filename_for_resource(resource: dict[str, Any], index: int) -> str:
    meta = resource.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("fileName"), str):
        return Path(meta["fileName"]).name
    uri = resource.get("uri")
    if isinstance(uri, str):
        name = Path(urllib.parse.urlparse(uri).path).name
        if name:
            return name
    return f"activity-{index}.fit"


def _save_resource_blobs(
    activity: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[Path, ...]:
    folder = _activity_fit_dir(activity)
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, resource in enumerate(_resource_items(payload), start=1):
        blob = resource.get("blob")
        if not isinstance(blob, str) or not blob:
            continue
        file_path = folder / _filename_for_resource(resource, index)
        file_path.write_bytes(base64.b64decode(blob))
        paths.append(file_path)

    if paths:
        metadata_path = folder / "metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "archived_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "activity": {
                        "labelId": activity.get("labelId"),
                        "sportType": activity.get("sportType"),
                        "date": _activity_date(activity),
                        "startTimestamp": activity.get("startTimestamp"),
                        "endTimestamp": activity.get("endTimestamp"),
                    },
                    "files": [path.name for path in paths],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return tuple(paths)


def _extract_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for text in _text_items(payload):
        urls.extend(re.findall(r"https?://\\S+", text))
        parsed = _parse_json_text(text)
        if parsed is not None:
            urls.extend(_extract_urls_from_json(parsed))
    return urls


def _parse_json_text(text: str) -> Any:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def _extract_urls_from_json(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
    elif isinstance(value, list):
        for item in value:
            urls.extend(_extract_urls_from_json(item))
    elif isinstance(value, dict):
        for item in value.values():
            urls.extend(_extract_urls_from_json(item))
    return urls


def _download_url(url: str, target: Path) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": "COROSRunningAgent/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        target.write_bytes(response.read())
    return target


async def archive_fit_for_activity(activity: dict[str, Any]) -> FitArchiveResult:
    if not fit_archive_enabled():
        return FitArchiveResult((), False, "FIT archive disabled.")

    existing = _existing_fit_paths(activity)
    if existing:
        return FitArchiveResult(existing, False, "FIT already archived.")

    arguments = _detail_args(activity)
    try:
        payload = await call_coros_tool("downloadActivityFitFiles", arguments)
        paths = _save_resource_blobs(activity, payload)
        if paths:
            return FitArchiveResult(paths, True, f"Archived {len(paths)} FIT file(s).")
    except Exception as exc:
        fallback_error = str(exc)
    else:
        fallback_error = "downloadActivityFitFiles returned no FIT blobs."

    try:
        payload = await call_coros_tool("queryActivityFitFileDownloadUrls", arguments)
        urls = _extract_urls(payload)
        folder = _activity_fit_dir(activity)
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, url in enumerate(urls[:1], start=1):
            paths.append(_download_url(url, folder / f"activity-{index}.fit"))
        if paths:
            return FitArchiveResult(tuple(paths), True, f"Archived {len(paths)} FIT file(s) from URL.")
    except Exception as exc:
        return FitArchiveResult((), False, f"FIT archive failed: {fallback_error}; fallback failed: {exc}")

    return FitArchiveResult((), False, f"FIT archive failed: {fallback_error}")


async def archive_fit_for_activities(
    activities: list[dict[str, Any]],
    limit: int | None = None,
) -> list[FitArchiveResult]:
    results: list[FitArchiveResult] = []
    max_count = limit if limit is not None else int(os.getenv("COROS_FIT_ARCHIVE_SYNC_LIMIT", "10"))
    for activity in activities[: max(max_count, 0)]:
        results.append(await archive_fit_for_activity(activity))
    return results


def _fit_route_points(path: Path) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with fitdecode.FitReader(str(path)) as fit_file:
            for frame in fit_file:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name != "record":
                    continue
                lat = frame.get_value("position_lat", fallback=None)
                lon = frame.get_value("position_long", fallback=None)
                if not isinstance(lat, int) or not isinstance(lon, int):
                    continue
                latitude = lat * SEMICIRCLE_TO_DEGREES
                longitude = lon * SEMICIRCLE_TO_DEGREES
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    points.append((latitude, longitude))
    return points


def _downsample_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) <= MAX_ROUTE_POINTS:
        return points
    stride = max(1, len(points) // (MAX_ROUTE_POINTS - 1))
    sampled = points[::stride]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled[:MAX_ROUTE_POINTS]


def _encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    output: list[str] = []
    previous_lat = 0
    previous_lon = 0

    for lat, lon in points:
        current_lat = int(round(lat * factor))
        current_lon = int(round(lon * factor))
        output.append(_encode_polyline_value(current_lat - previous_lat))
        output.append(_encode_polyline_value(current_lon - previous_lon))
        previous_lat = current_lat
        previous_lon = current_lon

    return "".join(output)


def _encode_polyline_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else value << 1
    chunks: list[str] = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


def _route_map_path(activity: dict[str, Any]) -> Path:
    return _route_map_dir() / f"{_activity_slug(activity)}.png"


def _mapbox_static_image_url(points: list[tuple[float, float]]) -> str:
    token = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MAPBOX_ACCESS_TOKEN is not configured.")

    style = os.getenv("MAPBOX_STYLE_ID", "mapbox/outdoors-v12").strip()
    width = int(os.getenv("MAPBOX_ROUTE_MAP_WIDTH", "900"))
    height = int(os.getenv("MAPBOX_ROUTE_MAP_HEIGHT", "600"))
    encoded_route = urllib.parse.quote(_encode_polyline(points), safe="")
    overlay = f"path-5+ff5c35-0.85({encoded_route})"
    return (
        f"https://api.mapbox.com/styles/v1/{style}/static/"
        f"{overlay}/auto/{width}x{height}@2x"
        f"?padding=70&attribution=true&logo=true&access_token={urllib.parse.quote(token)}"
    )


async def render_route_map_for_activity(activity: dict[str, Any]) -> RouteMapResult:
    if not route_map_enabled():
        return RouteMapResult(None, 0, "Route map disabled.")

    target = _route_map_path(activity)
    if target.exists():
        return RouteMapResult(target, 0, "Route map already rendered.")

    archive = await archive_fit_for_activity(activity)
    if not archive.paths:
        return RouteMapResult(None, 0, archive.message)

    route_points: list[tuple[float, float]] = []
    for path in archive.paths:
        route_points = _fit_route_points(path)
        if len(route_points) >= 2:
            break

    if len(route_points) < 2:
        return RouteMapResult(None, len(route_points), "No GPS route found in FIT file.")

    sampled = _downsample_points(route_points)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = _mapbox_static_image_url(sampled)
    _download_url(url, target)
    return RouteMapResult(target, len(route_points), f"Rendered route map from {len(route_points)} GPS points.")
