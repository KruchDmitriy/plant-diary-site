from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .airtable import (
    EVENT_FIELDS,
    PHOTO_FIELDS,
    PLANT_FIELDS,
    AirtableRepository,
    PhotoRecord,
    PlantRecord,
)
from .config import PROJECT_ROOT, Settings
from .domain import (
    PlantAPIError,
    load_protected_names,
    parse_files,
    parse_iso_date,
    public_image_url,
    rename_approval_sentence,
    validate_date_precision,
    validate_event_type,
    validate_photo_type,
)
from .media import YandexStorage, download_image, prepare_image


class SnapshotRepository:
    """Read-only fallback used while the API is in safe dry-run mode."""

    def __init__(self, data_dir: Path = PROJECT_ROOT / "data") -> None:
        self.data_dir = data_dir

    def plants(self) -> list[PlantRecord]:
        rows = self._load("plants.json")
        return [
            PlantRecord(
                record_id=f"snapshot-plant-{row['id']}",
                plant_id=int(row["id"]),
                name=str(row.get("name") or ""),
                latin_name="",
                cultivar="",
                group=str(row.get("group") or ""),
                status=str(row.get("status") or ""),
                notes="",
                date_added=None,
                main_photo_url=row.get("cover"),
            )
            for row in rows
        ]

    def plant_by_id(self, plant_id: int) -> PlantRecord:
        for plant in self.plants():
            if plant.plant_id == plant_id:
                return plant
        raise PlantAPIError(f"Plant ID #{plant_id} was not found", status_code=404, code="not_found")

    def photos(self, plants_by_record_id: dict[str, int] | None = None) -> list[PhotoRecord]:
        rows = self._load("photos.json")
        return [
            PhotoRecord(
                record_id=f"snapshot-photo-{row.get('index')}",
                filename=str(row.get("filename") or ""),
                source_file_id="",
                plant_ids=tuple(int(item) for item in row.get("plant_ids", [])),
                plant_record_ids=tuple(),
                date=row.get("date"),
                photo_type=str(row.get("type") or ""),
                comment=str(row.get("note") or ""),
                web_key=str(row.get("web_key") or ""),
                url=str(row.get("url") or ""),
                upload_status="Загружено",
            )
            for row in rows
        ]

    def _load(self, filename: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads((self.data_dir / filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlantAPIError(f"Cannot read local snapshot: {exc}", status_code=500) from exc
        if not isinstance(payload, list):
            raise PlantAPIError(f"{filename} is not a JSON array", status_code=500)
        return payload


class GitHubDeployer:
    def __init__(self, settings: Settings, *, timeout: float = 15.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def trigger(self) -> str:
        if not self.settings.github_token:
            return "not_configured"
        repository = self.settings.github_repository
        workflow = quote(self.settings.github_workflow, safe="")
        url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches"
        request = Request(
            url,
            method="POST",
            data=b'{"ref":"main"}',
            headers={
                "Authorization": f"Bearer {self.settings.github_token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "plant-diary-gpt-action/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status not in {201, 204}:
                    return f"http_{response.status}"
        except (HTTPError, URLError, TimeoutError):
            return "failed"
        return "triggered"


class PlantDiaryService:
    _new_plant_lock = threading.Lock()

    def __init__(
        self,
        settings: Settings,
        *,
        repository: Any = None,
        storage: Any = None,
        deployer: Any = None,
    ) -> None:
        self.settings = settings
        if repository is not None:
            self.repository = repository
        elif settings.airtable_token:
            self.repository = AirtableRepository(settings)
        else:
            self.repository = SnapshotRepository()
        self.storage = storage or YandexStorage(settings)
        self.deployer = deployer or GitHubDeployer(settings)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "write_enabled": self.settings.write_enabled,
            "write_configuration_complete": not self.settings.missing_write_secrets(),
        }

    def search_plants(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        needle = str(query or "").strip().casefold().lstrip("#")
        if not needle:
            raise PlantAPIError("Search query is empty")
        matches: list[PlantRecord] = []
        for plant in self.repository.plants():
            exact_id = needle.isdigit() and plant.plant_id == int(needle)
            haystack = " ".join(
                [str(plant.plant_id), plant.name, plant.latin_name, plant.cultivar]
            ).casefold()
            if exact_id or needle in haystack:
                matches.append(plant)
        matches.sort(key=lambda item: (0 if needle.isdigit() and item.plant_id == int(needle) else 1, item.plant_id))
        return {"query": query, "plants": [self._plant_json(item) for item in matches[:limit]]}

    def get_plant(self, plant_id: int) -> dict[str, Any]:
        plant = self.repository.plant_by_id(plant_id)
        result = self._plant_json(plant)
        if result["main_photo_url"] is None:
            photos = [
                photo
                for photo in self.repository.photos()
                if plant_id in photo.plant_ids and (photo.url or photo.web_key)
            ]
            if photos:
                cover = max(photos, key=self._automatic_cover_score)
                result["main_photo_url"] = cover.url or public_image_url(
                    self.settings.public_image_root, cover.web_key
                )
        return result

    def list_plant_photos(self, plant_id: int) -> dict[str, Any]:
        self.repository.plant_by_id(plant_id)
        photos = [photo for photo in self.repository.photos() if plant_id in photo.plant_ids]
        photos.sort(key=lambda item: (item.date or "", item.web_key, item.record_id), reverse=True)
        return {
            "plant_id": plant_id,
            "photos": [
                {
                    "photo_record_id": item.record_id,
                    "filename": item.filename,
                    "date": item.date,
                    "type": item.photo_type,
                    "comment": item.comment,
                    "web_key": item.web_key,
                    "url": item.url or public_image_url(self.settings.public_image_root, item.web_key),
                    "is_group_photo": len(item.plant_ids) > 1,
                    "linked_plant_ids": list(item.plant_ids),
                }
                for item in photos
            ],
        }

    def add_photos(self, plant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plant = self.repository.plant_by_id(plant_id)
        refs = parse_files(payload.get("openaiFileIdRefs"), max_files=self.settings.max_files)
        photo_date = parse_iso_date(payload.get("date"), "date")
        photo_type = validate_photo_type(payload.get("type"))
        comment = str(payload.get("comment") or "").strip()
        cover_file_id = str(payload.get("cover_file_id") or "").strip() or None
        if cover_file_id and cover_file_id not in {item.file_id for item in refs}:
            raise PlantAPIError("cover_file_id must refer to one of the attached files")
        plan = {
            "operation": "add_photos",
            "plant": self._plant_json(plant),
            "file_count": len(refs),
            "filenames": [item.name for item in refs],
            "date": photo_date,
            "type": photo_type,
            "comment": comment or None,
            "cover_file_id": cover_file_id,
        }
        gate = self._write_gate(payload, plan)
        if gate:
            return gate
        result = self._store_photos(
            plant,
            refs,
            photo_date=photo_date,
            photo_type=photo_type,
            comment=comment,
            cover_file_id=cover_file_id,
        )
        deploy = self.deployer.trigger()
        return {"status": "completed", **result, "deployment": deploy}

    def create_plant(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise PlantAPIError("Plant name is required")
        latin_name = str(payload.get("latin_name") or "").strip()
        cultivar = str(payload.get("cultivar") or "").strip()
        group = str(payload.get("group") or "").strip()
        status = str(payload.get("status") or "Наблюдение").strip()
        notes = str(payload.get("notes") or "").strip()
        date_added = parse_iso_date(payload.get("date_added"), "date_added")
        raw_refs = payload.get("openaiFileIdRefs") or []
        refs = parse_files(raw_refs, max_files=self.settings.max_files) if raw_refs else []
        photo_type = validate_photo_type(payload.get("photo_type")) if refs else "Состояние"
        photo_comment = str(payload.get("photo_comment") or "").strip()
        cover_file_id = str(payload.get("cover_file_id") or "").strip() or None
        if cover_file_id and cover_file_id not in {item.file_id for item in refs}:
            raise PlantAPIError("cover_file_id must refer to one of the attached files")
        acquisition = payload.get("acquisition_event")
        acquisition_plan = self._event_values(acquisition) if acquisition else None
        plants = self.repository.plants()
        next_id = max((item.plant_id for item in plants), default=0) + 1
        expected_id = self._confirmed_new_plant_id(payload, next_id)
        self._reject_existing_source_files(refs)
        plan = {
            "operation": "create_plant",
            "proposed_plant_id": next_id,
            "name": name,
            "latin_name": latin_name or None,
            "cultivar": cultivar or None,
            "group": group or None,
            "status": status,
            "date_added": date_added,
            "file_count": len(refs),
            "expected_plant_id": expected_id,
            "acquisition_event": acquisition_plan,
        }
        gate = self._write_gate(payload, plan)
        if gate:
            return gate

        with self._new_plant_lock:
            plants = self.repository.plants()
            next_id = max((item.plant_id for item in plants), default=0) + 1
            if expected_id != next_id:
                raise PlantAPIError(
                    f"The next Plant ID changed after confirmation: expected #{expected_id}, "
                    f"current next ID is #{next_id}",
                    status_code=409,
                    code="stale_plant_id",
                )
            self._reject_existing_source_files(refs)
            date_path = date_added or "undated"
            prepared = [
                prepare_image(
                    download_image(item, max_bytes=self.settings.max_file_bytes),
                    date_path=date_path,
                )
                for item in refs
            ]
            for image in prepared:
                self.storage.store(image, plant_id=next_id)

            fields: dict[str, Any] = {
                PLANT_FIELDS["name"]: name,
                PLANT_FIELDS["plant_id"]: next_id,
                PLANT_FIELDS["status"]: status,
            }
            optional_fields = {
                PLANT_FIELDS["latin_name"]: latin_name,
                PLANT_FIELDS["cultivar"]: cultivar,
                PLANT_FIELDS["group"]: group,
                PLANT_FIELDS["notes"]: notes,
                PLANT_FIELDS["date_added"]: date_added,
            }
            fields.update({key: value for key, value in optional_fields.items() if value})
            record_id = self.repository.create_plant(fields)
            plant = PlantRecord(
                record_id=record_id,
                plant_id=next_id,
                name=name,
                latin_name=latin_name,
                cultivar=cultivar,
                group=group,
                status=status,
                notes=notes,
                date_added=date_added,
                main_photo_url=None,
            )
            photo_result = self._create_prepared_photo_records(
                plant,
                prepared,
                photo_date=date_added,
                photo_type=photo_type,
                comment=photo_comment,
                cover_file_id=cover_file_id,
            )
            event_id = None
            if acquisition:
                event_id = self._create_event(plant, acquisition)

        deploy = self.deployer.trigger()
        return {
            "status": "completed",
            "plant": self._plant_json(plant),
            **photo_result,
            "event_record_id": event_id,
            "deployment": deploy,
        }

    def update_identity(self, plant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plant = self.repository.plant_by_id(plant_id)
        expected = str(payload.get("expected_current_name") or "").strip()
        if expected != plant.name:
            raise PlantAPIError(
                f"Plant name changed since confirmation: expected {expected!r}, current {plant.name!r}",
                status_code=409,
                code="stale_record",
            )
        new_name = str(payload.get("name") or "").strip()
        latin_name = payload.get("latin_name")
        cultivar = payload.get("cultivar")
        if not new_name and latin_name is None and cultivar is None:
            raise PlantAPIError("Provide name, latin_name, or cultivar")
        changes: dict[str, Any] = {}
        if new_name and new_name != plant.name:
            changes["name"] = {"from": plant.name, "to": new_name}
        if latin_name is not None and str(latin_name).strip() != plant.latin_name:
            changes["latin_name"] = {"from": plant.latin_name, "to": str(latin_name).strip()}
        if cultivar is not None and str(cultivar).strip() != plant.cultivar:
            changes["cultivar"] = {"from": plant.cultivar, "to": str(cultivar).strip()}
        if not changes:
            return {"status": "no_change", "plant": self._plant_json(plant)}
        plan = {"operation": "update_plant_identity", "plant_id": plant_id, "changes": changes}
        gate = self._write_gate(payload, plan)
        if gate:
            return gate

        protected = load_protected_names(self.settings.baseline_path)
        fields: dict[str, Any] = {}
        if "name" in changes:
            baseline_name = protected.get(plant_id, plant.name)
            final_name = changes["name"]["to"]
            description = rename_approval_sentence(baseline_name, final_name)
            if plant.name != baseline_name:
                description += f" Предыдущее значение в карточке: «{plant.name}»."
            self.repository.create_event(
                {
                    EVENT_FIELDS["title"]: "Уточнение названия",
                    EVENT_FIELDS["plant_id"]: plant_id,
                    EVENT_FIELDS["date"]: date.today().isoformat(),
                    EVENT_FIELDS["type"]: "Другое",
                    EVENT_FIELDS["description"]: description,
                    EVENT_FIELDS["plant_relation"]: [plant.record_id],
                    EVENT_FIELDS["date_precision"]: "Точная",
                }
            )
            fields[PLANT_FIELDS["name"]] = final_name
        if "latin_name" in changes:
            fields[PLANT_FIELDS["latin_name"]] = changes["latin_name"]["to"] or None
        if "cultivar" in changes:
            fields[PLANT_FIELDS["cultivar"]] = changes["cultivar"]["to"] or None
        self.repository.update_plant(plant.record_id, fields)
        deploy = self.deployer.trigger()
        return {"status": "completed", "plant_id": plant_id, "changes": changes, "deployment": deploy}

    def set_cover(self, plant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plant = self.repository.plant_by_id(plant_id)
        photo_record_id = str(payload.get("photo_record_id") or "").strip()
        web_key = str(payload.get("web_key") or "").strip()
        if bool(photo_record_id) == bool(web_key):
            raise PlantAPIError("Provide exactly one of photo_record_id or web_key")
        candidates = [photo for photo in self.repository.photos() if plant_id in photo.plant_ids]
        matches = [
            item
            for item in candidates
            if (photo_record_id and item.record_id == photo_record_id) or (web_key and item.web_key == web_key)
        ]
        if len(matches) != 1:
            raise PlantAPIError(
                "Choose exactly one uploaded photo linked to this plant",
                status_code=404,
                code="photo_not_found",
            )
        photo = matches[0]
        if photo.upload_status != "Загружено" or not photo.web_key:
            raise PlantAPIError("The selected photo is not fully uploaded", status_code=409)
        url = photo.url or public_image_url(self.settings.public_image_root, photo.web_key)
        plan = {
            "operation": "set_cover",
            "plant": self._plant_json(plant),
            "photo_record_id": photo.record_id,
            "url": url,
            "is_group_photo": len(photo.plant_ids) > 1,
        }
        if len(photo.plant_ids) > 1 and payload.get("allow_group_photo") is not True:
            return {"status": "group_photo_confirmation_required", "planned_change": plan}
        gate = self._write_gate(payload, plan)
        if gate:
            return gate
        self.repository.update_plant(plant.record_id, {PLANT_FIELDS["main_photo_url"]: url})
        deploy = self.deployer.trigger()
        return {"status": "completed", **plan, "deployment": deploy}

    def clear_cover(self, plant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plant = self.repository.plant_by_id(plant_id)
        plan = {"operation": "clear_cover", "plant": self._plant_json(plant)}
        gate = self._write_gate(payload, plan)
        if gate:
            return gate
        self.repository.update_plant(plant.record_id, {PLANT_FIELDS["main_photo_url"]: None})
        deploy = self.deployer.trigger()
        return {"status": "completed", **plan, "deployment": deploy}

    def add_event(self, plant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        plant = self.repository.plant_by_id(plant_id)
        values = self._event_values(payload)
        plan = {
            "operation": "add_event",
            "plant": self._plant_json(plant),
            **values,
        }
        gate = self._write_gate(payload, plan)
        if gate:
            return gate
        record_id = self._create_event(plant, payload)
        deploy = self.deployer.trigger()
        return {"status": "completed", **plan, "event_record_id": record_id, "deployment": deploy}

    def _store_photos(
        self,
        plant: PlantRecord,
        refs: list[Any],
        *,
        photo_date: str | None,
        photo_type: str,
        comment: str,
        cover_file_id: str | None,
    ) -> dict[str, Any]:
        known: dict[str, PhotoRecord] = {}
        for photo in self.repository.photos():
            if not photo.source_file_id:
                continue
            if photo.source_file_id in known:
                raise PlantAPIError(
                    f"Duplicate Source file ID in Airtable: {photo.source_file_id}",
                    status_code=409,
                    code="duplicate_source_file_id",
                )
            known[photo.source_file_id] = photo
        new_refs = []
        existing_results = []
        existing_cover_url = None
        for ref in refs:
            existing = known.get(ref.file_id)
            if existing:
                if plant.plant_id not in existing.plant_ids:
                    raise PlantAPIError(
                        f"File {ref.file_id} is already linked to another plant",
                        status_code=409,
                        code="duplicate_file",
                    )
                if existing.upload_status != "Загружено" or not existing.web_key:
                    raise PlantAPIError(
                        f"File {ref.file_id} already exists but is not fully uploaded",
                        status_code=409,
                        code="incomplete_photo",
                    )
                existing_results.append(
                    {
                        "source_file_id": ref.file_id,
                        "photo_record_id": existing.record_id,
                        "web_key": existing.web_key,
                        "status": "already_present",
                    }
                )
                if cover_file_id == ref.file_id:
                    if len(existing.plant_ids) > 1:
                        raise PlantAPIError(
                            "An existing group photo must be selected through setPlantCover",
                            status_code=409,
                            code="group_photo_confirmation_required",
                        )
                    existing_cover_url = existing.url or public_image_url(
                        self.settings.public_image_root, existing.web_key
                    )
            else:
                new_refs.append(ref)
        date_path = photo_date or "undated"
        prepared = [
            prepare_image(
                download_image(item, max_bytes=self.settings.max_file_bytes),
                date_path=date_path,
            )
            for item in new_refs
        ]
        for image in prepared:
            self.storage.store(image, plant_id=plant.plant_id)
        created = self._create_prepared_photo_records(
            plant,
            prepared,
            photo_date=photo_date,
            photo_type=photo_type,
            comment=comment,
            cover_file_id=cover_file_id,
        )
        if existing_cover_url:
            self.repository.update_plant(
                plant.record_id,
                {PLANT_FIELDS["main_photo_url"]: existing_cover_url},
            )
            created["cover_updated"] = True
            created["cover_url"] = existing_cover_url
        created["photos"] = existing_results + created["photos"]
        return created

    def _reject_existing_source_files(self, refs: list[Any]) -> None:
        if not refs:
            return
        requested = {item.file_id for item in refs}
        existing = sorted(
            {
                photo.source_file_id
                for photo in self.repository.photos()
                if photo.source_file_id in requested
            }
        )
        if existing:
            raise PlantAPIError(
                "Attached files already belong to the diary; refusing to create a duplicate "
                f"plant: {', '.join(existing)}",
                status_code=409,
                code="possible_duplicate_create",
            )

    @staticmethod
    def _confirmed_new_plant_id(payload: dict[str, Any], proposed_id: int) -> int | None:
        if payload.get("confirmed") is not True:
            return None
        value = payload.get("expected_plant_id")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PlantAPIError(
                "expected_plant_id is required after confirming a new plant",
                status_code=409,
                code="plant_id_confirmation_required",
            )
        if value != proposed_id:
            raise PlantAPIError(
                f"The next Plant ID changed after confirmation: expected #{value}, "
                f"current next ID is #{proposed_id}",
                status_code=409,
                code="stale_plant_id",
            )
        return value

    def _create_prepared_photo_records(
        self,
        plant: PlantRecord,
        prepared: list[Any],
        *,
        photo_date: str | None,
        photo_type: str,
        comment: str,
        cover_file_id: str | None,
    ) -> dict[str, Any]:
        results = []
        cover_url = None
        for image in prepared:
            url = public_image_url(self.settings.public_image_root, image.web_key)
            fields: dict[str, Any] = {
                PHOTO_FIELDS["filename"]: image.source.ref.name,
                PHOTO_FIELDS["plant_id"]: plant.plant_id,
                PHOTO_FIELDS["object_key"]: image.original_key,
                PHOTO_FIELDS["url"]: url,
                PHOTO_FIELDS["type"]: photo_type,
                PHOTO_FIELDS["source_file_id"]: image.source.ref.file_id,
                PHOTO_FIELDS["plants_relation"]: [plant.record_id],
                PHOTO_FIELDS["web_key"]: image.web_key,
                PHOTO_FIELDS["sha256"]: image.source.sha256,
                PHOTO_FIELDS["upload_status"]: "Загружено",
            }
            if photo_date:
                fields[PHOTO_FIELDS["date"]] = photo_date
            if comment:
                fields[PHOTO_FIELDS["comment"]] = comment
            record_id = self.repository.create_photo(fields)
            results.append(
                {
                    "source_file_id": image.source.ref.file_id,
                    "photo_record_id": record_id,
                    "web_key": image.web_key,
                    "url": url,
                    "status": "created",
                }
            )
            if cover_file_id == image.source.ref.file_id:
                cover_url = url
        if cover_url:
            self.repository.update_plant(plant.record_id, {PLANT_FIELDS["main_photo_url"]: cover_url})
        return {
            "plant_id": plant.plant_id,
            "photos": results,
            "cover_updated": bool(cover_url),
            "cover_url": cover_url,
        }

    def _create_event(self, plant: PlantRecord, payload: dict[str, Any]) -> str:
        values = self._event_values(payload)
        fields: dict[str, Any] = {
            EVENT_FIELDS["title"]: values["title"],
            EVENT_FIELDS["plant_id"]: plant.plant_id,
            EVENT_FIELDS["type"]: values["type"],
            EVENT_FIELDS["description"]: values["description"] or "",
            EVENT_FIELDS["plant_relation"]: [plant.record_id],
            EVENT_FIELDS["date_precision"]: values["date_precision"],
        }
        if values["date"]:
            fields[EVENT_FIELDS["date"]] = values["date"]
        if values["period"]:
            fields[EVENT_FIELDS["period"]] = values["period"]
        return self.repository.create_event(fields)

    @staticmethod
    def _event_values(payload: dict[str, Any]) -> dict[str, Any]:
        event_type = validate_event_type(payload.get("type"))
        event_date = parse_iso_date(payload.get("date"), "date")
        precision = validate_date_precision(payload.get("date_precision"))
        period = str(payload.get("period") or "").strip()
        if not event_date and not period:
            raise PlantAPIError("An event needs either an exact date or an approximate period")
        return {
            "type": event_type,
            "date": event_date,
            "date_precision": precision,
            "period": period or None,
            "title": str(payload.get("title") or event_type).strip(),
            "description": str(payload.get("description") or "").strip() or None,
        }

    def _write_gate(self, payload: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("confirmed") is not True:
            return {"status": "confirmation_required", "planned_change": plan}
        if not self.settings.write_enabled:
            return {"status": "dry_run", "planned_change": plan}
        missing = self.settings.missing_write_secrets()
        if missing:
            raise PlantAPIError(
                f"Write mode is missing configuration: {', '.join(missing)}",
                status_code=503,
                code="not_configured",
            )
        return None

    @staticmethod
    def _plant_json(plant: PlantRecord) -> dict[str, Any]:
        return {
            "plant_id": plant.plant_id,
            "name": plant.name,
            "latin_name": plant.latin_name or None,
            "cultivar": plant.cultivar or None,
            "group": plant.group or None,
            "status": plant.status or None,
            "date_added": plant.date_added,
            "main_photo_url": plant.main_photo_url,
        }

    @staticmethod
    def _automatic_cover_score(photo: PhotoRecord) -> tuple[int, str, str]:
        if photo.photo_type == "Основное":
            priority = 3
        elif photo.photo_type == "Референс продавца":
            priority = 0
        else:
            priority = 2
        return priority, photo.date or "", photo.web_key
