#!/usr/bin/env python3
"""Build the public plant diary JSON snapshot from Airtable.

The script is deliberately read-only with respect to Airtable. It fetches all
pages, normalizes the three tables, validates the complete snapshot, and only
then atomically replaces the public JSON files.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_BASELINE = DEFAULT_DATA_DIR / "sync_baseline.json"

AIRTABLE_API_ROOT = "https://api.airtable.com/v0"
DEFAULT_BASE_ID = "appnyj7AhSHhW4PUe"
DEFAULT_TABLE_IDS = {
    "plants": "tbljZlrQNMTpq4E2g",
    "photos": "tblswsaSHsGjBp8E9",
    "events": "tblqFSe8BKHSl8yp1",
}
PUBLIC_IMAGE_ROOT = "https://storage.yandexcloud.net/plant-diary-photos"

PLANT_FIELDS = {
    "name": ("fldQIch5luihYQSG8", "Название"),
    "plant_id": ("fld308cP1obisCF7T", "Plant ID"),
    "group": ("fldDNGm2g0pzpibb2", "Группа"),
    "status": ("fld82SQ9bc2V7qbPs", "Статус"),
    "main_photo_url": ("flddI0zmvTFslXD7M", "Главное фото URL"),
}

PHOTO_FIELDS = {
    "filename": ("fldljPY8THwO2VNlT", "Фото"),
    "plant_id": ("fldZC4GOgrGYlRiPs", "Plant ID"),
    "date": ("fld1hesrOjxVTU9qL", "Дата"),
    "object_key": ("fldTmnZ1MqERDne7q", "Object key"),
    "url": ("fldhdPNczccu9VxKG", "URL"),
    "type": ("fldIljVETgPDqDWrF", "Тип"),
    "note": ("fld1FPCbiTeipSCCT", "Комментарий"),
    "plants_relation": ("fldjwjT8931nKOZBU", "Растения"),
    "web_key": ("fld8zuqa2txCiwwJ6", "Web key"),
    "sha256": ("fldOGZCmp5oCzirQD", "SHA-256"),
    "upload_status": ("fldsNUxWJKPzINvin", "Статус загрузки"),
}

EVENT_FIELDS = {
    "title": ("fldJgSm8BKbXp9TJf", "Событие"),
    "plant_id": ("fldTm44mZBzdZLu2x", "Plant ID"),
    "date": ("fld7C3tzn70F2rsAj", "Дата"),
    "type": ("fldJvsojHcX6K6gKN", "Тип"),
    "description": ("fldDhRgJrZzPMR5xh", "Описание"),
    "plant_relation": ("fldNZurdCW1X8Gxdz", "Растение"),
    "date_precision": ("fldFnUF0px1WlDEpG", "Точность даты"),
    "period": ("fldlGNXWjqBNLQ9fs", "Период / уточнение даты"),
}

OUTPUT_FILES = {
    "plants": "plants.json",
    "photos": "photos.json",
    "events": "events.json",
}


class SyncError(RuntimeError):
    """A safe, user-facing sync failure."""


def field(record: dict[str, Any], spec: tuple[str, str], default: Any = None) -> Any:
    """Read a field by stable ID, with its display name as a compatibility fallback."""

    values = record.get("fields", {})
    field_id, field_name = spec
    return values.get(field_id, values.get(field_name, default))


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "").strip()
    return str(value).strip()


def optional_text(value: Any) -> str | None:
    value = text_value(value)
    return value or None


def date_value(value: Any, context: str) -> str | None:
    raw = optional_text(value)
    if raw is None:
        return None
    candidate = raw[:10]
    try:
        date.fromisoformat(candidate)
    except ValueError as exc:
        raise SyncError(f"{context}: invalid ISO date {raw!r}") from exc
    return candidate


def plant_id_value(value: Any, context: str, *, required: bool = True) -> int | None:
    if value is None or value == "":
        if required:
            raise SyncError(f"{context}: Plant ID is empty")
        return None
    if isinstance(value, bool):
        raise SyncError(f"{context}: Plant ID must be an integer, got boolean")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        result = int(value)
    else:
        raise SyncError(f"{context}: Plant ID must be an integer, got {value!r}")
    if result <= 0:
        raise SyncError(f"{context}: Plant ID must be positive, got {result}")
    return result


def relation_ids(value: Any, context: str) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SyncError(f"{context}: expected an Airtable record-link list")
    return value


def public_image_url(web_key: str) -> str:
    encoded_key = quote(web_key, safe="/!$&'()*+,;=:@-._~")
    return f"{PUBLIC_IMAGE_ROOT}/{encoded_key}"


def web_key_from_url(url: str | None) -> str | None:
    if not url:
        return None
    root = urlparse(PUBLIC_IMAGE_ROOT)
    parsed = urlparse(url)
    prefix = root.path.rstrip("/") + "/"
    if parsed.scheme == root.scheme and parsed.netloc == root.netloc and parsed.path.startswith(prefix):
        return parsed.path[len(prefix) :]
    return None


class AirtableClient:
    def __init__(self, token: str, base_id: str, timeout: float = 30.0) -> None:
        self.token = token
        self.base_id = base_id
        self.timeout = timeout
        self._last_request_at = 0.0

    def list_records(self, table_id: str, field_specs: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset: str | None = None
        field_ids = [field_id for field_id, _ in field_specs]

        while True:
            params: list[tuple[str, str]] = [
                ("pageSize", "100"),
                ("returnFieldsByFieldId", "true"),
            ]
            params.extend(("fields[]", field_id) for field_id in field_ids)
            if offset:
                params.append(("offset", offset))
            endpoint = (
                f"{AIRTABLE_API_ROOT}/{quote(self.base_id, safe='')}/"
                f"{quote(table_id, safe='')}?{urlencode(params)}"
            )
            payload = self._get_json(endpoint)
            page = payload.get("records")
            if not isinstance(page, list):
                raise SyncError(f"Airtable table {table_id}: response has no records list")
            records.extend(page)
            next_offset = payload.get("offset")
            if not next_offset:
                return records
            if not isinstance(next_offset, str) or next_offset == offset:
                raise SyncError(f"Airtable table {table_id}: invalid pagination offset")
            offset = next_offset

    def _get_json(self, url: str) -> dict[str, Any]:
        for attempt in range(5):
            delay = 0.22 - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)

            request = Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "User-Agent": "plant-diary-airtable-sync/1.0",
                },
            )
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise SyncError("Airtable returned a non-object JSON response")
                return payload
            except HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 4:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        wait_seconds = float(retry_after) if retry_after else 2**attempt
                    except ValueError:
                        wait_seconds = 2**attempt
                    time.sleep(min(max(wait_seconds, 1.0), 30.0))
                    continue
                try:
                    body = exc.read(512).decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                detail = f": {body}" if body else ""
                raise SyncError(f"Airtable request failed with HTTP {exc.code}{detail}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 4:
                    time.sleep(2**attempt)
                    continue
                raise SyncError(f"Airtable request failed: {exc}") from exc
        raise AssertionError("retry loop ended unexpectedly")


def normalize_snapshot(
    plant_records: list[dict[str, Any]],
    photo_records: list[dict[str, Any]],
    event_records: list[dict[str, Any]],
    existing_photos: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    legacy_photo_by_key = {
        text_value(photo.get("web_key")): photo
        for photo in (existing_photos or [])
        if text_value(photo.get("web_key"))
    }
    plant_rows: list[dict[str, Any]] = []
    plant_record_to_id: dict[str, int] = {}

    for record in plant_records:
        record_id = text_value(record.get("id"))
        context = f"plant record {record_id or '<unknown>'}"
        if not record_id:
            raise SyncError(f"{context}: Airtable record ID is empty")
        plant_id = plant_id_value(field(record, PLANT_FIELDS["plant_id"]), context)
        assert plant_id is not None
        if record_id in plant_record_to_id:
            raise SyncError(f"{context}: duplicate Airtable record ID")
        plant_record_to_id[record_id] = plant_id
        name = text_value(field(record, PLANT_FIELDS["name"]))
        if not name:
            raise SyncError(f"{context}: name is empty")
        plant_rows.append(
            {
                "_record_id": record_id,
                "id": plant_id,
                "name": name,
                "status": text_value(field(record, PLANT_FIELDS["status"])),
                "group": text_value(field(record, PLANT_FIELDS["group"])),
                "_main_photo_url": optional_text(field(record, PLANT_FIELDS["main_photo_url"])),
            }
        )

    photo_rows: list[dict[str, Any]] = []
    for record in photo_records:
        record_id = text_value(record.get("id"))
        context = f"photo record {record_id or '<unknown>'}"
        if not record_id:
            raise SyncError(f"{context}: Airtable record ID is empty")
        relations = relation_ids(field(record, PHOTO_FIELDS["plants_relation"]), context)
        unknown_relations = [item for item in relations if item not in plant_record_to_id]
        if unknown_relations:
            raise SyncError(f"{context}: links unknown plant records {unknown_relations}")
        linked_plant_ids = sorted({plant_record_to_id[item] for item in relations})

        scalar_plant_id = plant_id_value(
            field(record, PHOTO_FIELDS["plant_id"]), context, required=False
        )
        if scalar_plant_id is not None and not linked_plant_ids:
            raise SyncError(f"{context}: has Plant ID #{scalar_plant_id} but no plant relation")
        if scalar_plant_id is not None and scalar_plant_id not in linked_plant_ids:
            raise SyncError(
                f"{context}: scalar Plant ID #{scalar_plant_id} contradicts linked plants {linked_plant_ids}"
            )

        web_key = text_value(field(record, PHOTO_FIELDS["web_key"]))
        object_key = text_value(field(record, PHOTO_FIELDS["object_key"]))
        photo_cell = field(record, PHOTO_FIELDS["filename"])
        attachment = photo_cell[0] if isinstance(photo_cell, list) and photo_cell else {}
        if not isinstance(attachment, dict):
            attachment = {}
        filename = text_value(photo_cell) if isinstance(photo_cell, str) else text_value(attachment.get("filename"))
        if not filename:
            filename = posixpath.basename(object_key or web_key)
        legacy_photo = legacy_photo_by_key.get(web_key, {})

        photo_rows.append(
            {
                "_record_id": record_id,
                "_upload_status": text_value(field(record, PHOTO_FIELDS["upload_status"])),
                "filename": filename,
                "date": date_value(field(record, PHOTO_FIELDS["date"]), context),
                "type": text_value(field(record, PHOTO_FIELDS["type"])),
                "plant_ids": linked_plant_ids,
                "web_key": web_key,
                "url": public_image_url(web_key) if web_key else "",
                "sha256": text_value(field(record, PHOTO_FIELDS["sha256"])),
                "width": attachment.get("width", legacy_photo.get("width")),
                "height": attachment.get("height", legacy_photo.get("height")),
                "note": text_value(field(record, PHOTO_FIELDS["note"])),
                "_legacy_index": legacy_photo.get("index"),
            }
        )

    photo_rows.sort(key=lambda item: (item["web_key"].casefold(), item["_record_id"]))
    used_indices: set[int] = set()
    for photo in photo_rows:
        legacy_index = photo["_legacy_index"]
        if (
            isinstance(legacy_index, int)
            and not isinstance(legacy_index, bool)
            and legacy_index > 0
            and legacy_index not in used_indices
        ):
            photo["index"] = legacy_index
            used_indices.add(legacy_index)
    next_index = max(used_indices, default=0) + 1
    for photo in photo_rows:
        if "index" not in photo:
            while next_index in used_indices:
                next_index += 1
            photo["index"] = next_index
            used_indices.add(next_index)
            next_index += 1
    photo_rows.sort(key=lambda item: item["index"])

    event_rows: list[dict[str, Any]] = []
    for record in event_records:
        record_id = text_value(record.get("id"))
        context = f"event record {record_id or '<unknown>'}"
        if not record_id:
            raise SyncError(f"{context}: Airtable record ID is empty")
        relations = relation_ids(field(record, EVENT_FIELDS["plant_relation"]), context)
        if len(relations) != 1:
            raise SyncError(f"{context}: expected exactly one linked plant, got {len(relations)}")
        if relations[0] not in plant_record_to_id:
            raise SyncError(f"{context}: links unknown plant record {relations[0]}")
        linked_plant_id = plant_record_to_id[relations[0]]
        scalar_plant_id = plant_id_value(field(record, EVENT_FIELDS["plant_id"]), context)
        if scalar_plant_id != linked_plant_id:
            raise SyncError(
                f"{context}: scalar Plant ID #{scalar_plant_id} contradicts linked plant #{linked_plant_id}"
            )
        event_rows.append(
            {
                "_record_id": record_id,
                "plant_id": linked_plant_id,
                "date": date_value(field(record, EVENT_FIELDS["date"]), context),
                "type": text_value(field(record, EVENT_FIELDS["type"])),
                "title": text_value(field(record, EVENT_FIELDS["title"])),
                "description": text_value(field(record, EVENT_FIELDS["description"])),
                "date_precision": text_value(field(record, EVENT_FIELDS["date_precision"])),
                "period": optional_text(field(record, EVENT_FIELDS["period"])),
            }
        )

    event_rows.sort(
        key=lambda item: (
            item["plant_id"],
            item["date"] or "9999-12-31",
            item["title"].casefold(),
            item["_record_id"],
        )
    )
    for event_id, event in enumerate(event_rows, start=1):
        event["id"] = event_id

    photo_counts = Counter(
        plant_id for photo in photo_rows for plant_id in photo["plant_ids"]
    )
    event_counts = Counter(event["plant_id"] for event in event_rows)
    photos_by_plant: dict[int, list[dict[str, Any]]] = {}
    for photo in photo_rows:
        for plant_id in photo["plant_ids"]:
            photos_by_plant.setdefault(plant_id, []).append(photo)

    plants: list[dict[str, Any]] = []
    for plant in sorted(plant_rows, key=lambda item: item["id"]):
        plant_id = plant["id"]
        cover_url = plant["_main_photo_url"]
        cover_key = web_key_from_url(cover_url)
        if not cover_url:
            candidates = photos_by_plant.get(plant_id, [])

            def cover_score(photo: dict[str, Any]) -> tuple[int, str, str]:
                photo_type = photo["type"]
                priority = 3 if photo_type == "Основное" else 0 if photo_type == "Референс продавца" else 2
                return priority, photo["date"] or "", photo["web_key"]

            if candidates:
                cover_photo = max(candidates, key=cover_score)
                cover_url = cover_photo["url"]
                cover_key = cover_photo["web_key"]
        plants.append(
            {
                "id": plant_id,
                "name": plant["name"],
                "status": plant["status"],
                "group": plant["group"],
                "photo_count": photo_counts[plant_id],
                "cover": cover_url,
                "cover_key": cover_key,
                "event_count": event_counts[plant_id],
            }
        )

    photos = [
        {key: value for key, value in photo.items() if not key.startswith("_")}
        for photo in photo_rows
    ]
    events = [
        {key: value for key, value in event.items() if not key.startswith("_")}
        for event in event_rows
    ]
    return plants, photos, events


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read {path}: {exc}") from exc


def load_snapshot(data_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    values = [load_json(data_dir / OUTPUT_FILES[name]) for name in ("plants", "photos", "events")]
    if not all(isinstance(value, list) for value in values):
        raise SyncError("Snapshot files must each contain a JSON array")
    return values[0], values[1], values[2]


def validate_snapshot(
    plants: list[dict[str, Any]],
    photos: list[dict[str, Any]],
    events: list[dict[str, Any]],
    baseline: dict[str, Any],
    *,
    upload_statuses: list[str] | None = None,
) -> dict[str, int]:
    errors: list[str] = []
    counts = {"plants": len(plants), "photos": len(photos), "events": len(events)}

    minimum_counts = baseline.get("minimum_counts", {})
    for name, count in counts.items():
        minimum = minimum_counts.get(name)
        if isinstance(minimum, int) and count < minimum:
            errors.append(f"{name}: expected at least {minimum}, got {count}")

    plant_ids: list[int] = []
    names_by_id: dict[int, str] = {}
    for position, plant in enumerate(plants, start=1):
        plant_id = plant.get("id")
        if isinstance(plant_id, bool) or not isinstance(plant_id, int) or plant_id <= 0:
            errors.append(f"plants[{position}]: invalid integer Plant ID {plant_id!r}")
            continue
        plant_ids.append(plant_id)
        names_by_id[plant_id] = text_value(plant.get("name"))

    duplicate_ids = sorted(plant_id for plant_id, count in Counter(plant_ids).items() if count > 1)
    if duplicate_ids:
        errors.append(f"duplicate Plant IDs: {duplicate_ids}")
    known_plant_ids = set(plant_ids)

    protected_ids: set[int] = set()
    for value in baseline.get("protected_plant_ids", []):
        if isinstance(value, int):
            protected_ids.add(value)
    for item in baseline.get("protected_plant_id_ranges", []):
        if isinstance(item, list) and len(item) == 2 and all(isinstance(value, int) for value in item):
            protected_ids.update(range(item[0], item[1] + 1))
    missing_protected = sorted(protected_ids - known_plant_ids)
    if missing_protected:
        errors.append(f"protected Plant IDs are missing: {missing_protected}")

    for raw_id, expected_name in baseline.get("protected_plant_names", {}).items():
        try:
            plant_id = int(raw_id)
        except (TypeError, ValueError):
            errors.append(f"baseline has invalid protected Plant ID {raw_id!r}")
            continue
        actual_name = names_by_id.get(plant_id)
        if actual_name is not None and actual_name != expected_name:
            errors.append(
                f"protected plant #{plant_id} was renamed: {actual_name!r} != {expected_name!r}"
            )

    web_keys: list[str] = []
    actual_photo_counts: Counter[int] = Counter()
    for position, photo in enumerate(photos, start=1):
        context = f"photos[{position}]"
        web_key = text_value(photo.get("web_key"))
        web_keys.append(web_key)
        if not web_key.startswith("web/"):
            errors.append(f"{context}: Web key must stay under web/*, got {web_key!r}")
        expected_url = public_image_url(web_key) if web_key else ""
        if photo.get("url") != expected_url:
            errors.append(f"{context}: URL is not canonical for its Web key")
        sha256 = text_value(photo.get("sha256"))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            errors.append(f"{context}: invalid SHA-256")
        linked_ids = photo.get("plant_ids")
        if not isinstance(linked_ids, list):
            errors.append(f"{context}: plant_ids must be a list")
            continue
        if len(linked_ids) != len(set(linked_ids)):
            errors.append(f"{context}: duplicate values in plant_ids")
        for plant_id in linked_ids:
            if isinstance(plant_id, bool) or not isinstance(plant_id, int):
                errors.append(f"{context}: non-integer linked Plant ID {plant_id!r}")
            elif plant_id not in known_plant_ids:
                errors.append(f"{context}: linked Plant ID #{plant_id} does not exist")
            else:
                actual_photo_counts[plant_id] += 1

    duplicate_web_keys = sorted(key for key, count in Counter(web_keys).items() if count > 1)
    if duplicate_web_keys:
        errors.append(f"duplicate Web keys: {duplicate_web_keys[:10]}")
    known_web_keys = set(web_keys)

    if upload_statuses is not None:
        invalid_statuses = Counter(status for status in upload_statuses if status != "Загружено")
        if invalid_statuses:
            errors.append(f"photos not marked Загружено: {dict(invalid_statuses)}")

    actual_event_counts: Counter[int] = Counter()
    for position, event in enumerate(events, start=1):
        context = f"events[{position}]"
        plant_id = event.get("plant_id")
        if isinstance(plant_id, bool) or not isinstance(plant_id, int) or plant_id not in known_plant_ids:
            errors.append(f"{context}: invalid linked Plant ID {plant_id!r}")
        else:
            actual_event_counts[plant_id] += 1
        event_date = event.get("date")
        period = optional_text(event.get("period"))
        if event_date:
            try:
                date.fromisoformat(str(event_date))
            except ValueError:
                errors.append(f"{context}: invalid ISO date {event_date!r}")
        elif not period:
            errors.append(f"{context}: missing both exact date and approximate period")

    for plant in plants:
        plant_id = plant.get("id")
        if plant_id not in known_plant_ids:
            continue
        if plant.get("photo_count") != actual_photo_counts[plant_id]:
            errors.append(
                f"plant #{plant_id}: photo_count={plant.get('photo_count')!r}, "
                f"actual={actual_photo_counts[plant_id]}"
            )
        if plant.get("event_count") != actual_event_counts[plant_id]:
            errors.append(
                f"plant #{plant_id}: event_count={plant.get('event_count')!r}, "
                f"actual={actual_event_counts[plant_id]}"
            )
        cover_key = optional_text(plant.get("cover_key"))
        if cover_key and cover_key not in known_web_keys:
            errors.append(f"plant #{plant_id}: cover_key does not exist in photos")

    if errors:
        preview = errors[:50]
        suffix = f"\n  ... and {len(errors) - len(preview)} more" if len(errors) > len(preview) else ""
        raise SyncError("Snapshot validation failed:\n  - " + "\n  - ".join(preview) + suffix)

    return {
        **counts,
        "min_plant_id": min(plant_ids) if plant_ids else 0,
        "max_plant_id": max(plant_ids) if plant_ids else 0,
        "unlinked_photos": sum(1 for photo in photos if not photo.get("plant_ids")),
        "multi_plant_photos": sum(1 for photo in photos if len(photo.get("plant_ids", [])) > 1),
    }


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def print_summary(stats: dict[str, int], prefix: str) -> None:
    print(
        f"{prefix}: {stats['plants']} plants "
        f"(Plant IDs {stats['min_plant_id']}–{stats['max_plant_id']}), "
        f"{stats['photos']} photos, {stats['events']} events; "
        f"{stats['unlinked_photos']} unlinked and "
        f"{stats['multi_plant_photos']} multi-plant photos"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the existing JSON files without contacting Airtable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate Airtable data without replacing JSON files",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline = load_json(args.baseline)
    if not isinstance(baseline, dict):
        raise SyncError("Sync baseline must be a JSON object")

    if args.validate_only:
        snapshot = load_snapshot(args.data_dir)
        stats = validate_snapshot(*snapshot, baseline)
        print_summary(stats, "Valid snapshot")
        return 0

    token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    if not token:
        raise SyncError("AIRTABLE_TOKEN is required (it is read only from the environment)")

    base_id = os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE_ID).strip()
    table_ids = {
        name: os.environ.get(f"AIRTABLE_{name.upper()}_TABLE_ID", default).strip()
        for name, default in DEFAULT_TABLE_IDS.items()
    }
    client = AirtableClient(token=token, base_id=base_id, timeout=args.timeout)

    plant_records = client.list_records(table_ids["plants"], PLANT_FIELDS.values())
    photo_records = client.list_records(table_ids["photos"], PHOTO_FIELDS.values())
    event_records = client.list_records(table_ids["events"], EVENT_FIELDS.values())
    existing_photos: list[dict[str, Any]] = []
    existing_photos_path = args.data_dir / OUTPUT_FILES["photos"]
    if existing_photos_path.exists():
        loaded_photos = load_json(existing_photos_path)
        if not isinstance(loaded_photos, list):
            raise SyncError(f"{existing_photos_path} must contain a JSON array")
        existing_photos = loaded_photos
    snapshot = normalize_snapshot(
        plant_records, photo_records, event_records, existing_photos=existing_photos
    )

    upload_statuses = [
        text_value(field(record, PHOTO_FIELDS["upload_status"])) for record in photo_records
    ]
    stats = validate_snapshot(*snapshot, baseline, upload_statuses=upload_statuses)
    print_summary(stats, "Valid Airtable snapshot")

    if args.dry_run:
        print("Dry run: JSON files were not changed")
        return 0

    for name, value in zip(("plants", "photos", "events"), snapshot):
        write_json_atomic(args.data_dir / OUTPUT_FILES[name], value)
    print(f"Updated JSON files in {args.data_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
