from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import Settings
from .domain import PlantAPIError


AIRTABLE_API_ROOT = "https://api.airtable.com/v0"

PLANT_FIELDS = {
    "name": "fldQIch5luihYQSG8",
    "plant_id": "fld308cP1obisCF7T",
    "latin_name": "fldnriJFtPp3yStUA",
    "cultivar": "fld4P0KYzScaVExcW",
    "group": "fldDNGm2g0pzpibb2",
    "status": "fld82SQ9bc2V7qbPs",
    "notes": "fld7oZmPA6JfaTnhU",
    "date_added": "fldAFZsGWF3EwmdkI",
    "main_photo_url": "flddI0zmvTFslXD7M",
}

PHOTO_FIELDS = {
    "filename": "fldljPY8THwO2VNlT",
    "plant_id": "fldZC4GOgrGYlRiPs",
    "date": "fld1hesrOjxVTU9qL",
    "object_key": "fldTmnZ1MqERDne7q",
    "url": "fldhdPNczccu9VxKG",
    "type": "fldIljVETgPDqDWrF",
    "comment": "fld1FPCbiTeipSCCT",
    "source_file_id": "fld5ddHBq7qPfXuoq",
    "plants_relation": "fldjwjT8931nKOZBU",
    "web_key": "fld8zuqa2txCiwwJ6",
    "sha256": "fldOGZCmp5oCzirQD",
    "upload_status": "fldsNUxWJKPzINvin",
}

EVENT_FIELDS = {
    "title": "fldJgSm8BKbXp9TJf",
    "plant_id": "fldTm44mZBzdZLu2x",
    "date": "fld7C3tzn70F2rsAj",
    "type": "fldJvsojHcX6K6gKN",
    "description": "fldDhRgJrZzPMR5xh",
    "plant_relation": "fldNZurdCW1X8Gxdz",
    "date_precision": "fldFnUF0px1WlDEpG",
    "period": "fldlGNXWjqBNLQ9fs",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "").strip()
    return str(value).strip()


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise PlantAPIError(f"{context} has an invalid Plant ID", status_code=502)
    if isinstance(value, int):
        result = value
    if isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    elif not isinstance(value, int):
        raise PlantAPIError(f"{context} has an invalid Plant ID", status_code=502)
    if result <= 0:
        raise PlantAPIError(f"{context} has a non-positive Plant ID", status_code=502)
    return result


def _record_id(value: Any, context: str) -> str:
    result = _text(value)
    if not result.startswith("rec"):
        raise PlantAPIError(f"{context} has an invalid Airtable record ID", status_code=502)
    return result


@dataclass(frozen=True)
class PlantRecord:
    record_id: str
    plant_id: int
    name: str
    latin_name: str
    cultivar: str
    group: str
    status: str
    notes: str
    date_added: str | None
    main_photo_url: str | None


@dataclass(frozen=True)
class PhotoRecord:
    record_id: str
    filename: str
    source_file_id: str
    plant_ids: tuple[int, ...]
    plant_record_ids: tuple[str, ...]
    date: str | None
    photo_type: str
    comment: str
    web_key: str
    url: str
    upload_status: str


class AirtableHTTPClient:
    def __init__(self, token: str, base_id: str, *, timeout: float = 25.0) -> None:
        self.token = token
        self.base_id = base_id
        self.timeout = timeout
        self._last_request_at = 0.0

    def list_records(self, table_id: str, field_ids: Iterable[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            params: list[tuple[str, str]] = [
                ("pageSize", "100"),
                ("returnFieldsByFieldId", "true"),
            ]
            params.extend(("fields[]", field_id) for field_id in field_ids)
            if offset:
                params.append(("offset", offset))
            payload = self._request("GET", table_id, query=params)
            page = payload.get("records")
            if not isinstance(page, list):
                raise PlantAPIError("Airtable returned no records list", status_code=502)
            result.extend(page)
            next_offset = payload.get("offset")
            if not next_offset:
                return result
            if not isinstance(next_offset, str) or next_offset == offset:
                raise PlantAPIError("Airtable returned an invalid page cursor", status_code=502)
            offset = next_offset

    def create_record(self, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            table_id,
            body={"records": [{"fields": fields}], "typecast": False},
        )
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != 1:
            raise PlantAPIError("Airtable did not return the created record", status_code=502)
        return records[0]

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            table_id,
            body={"records": [{"id": record_id, "fields": fields}], "typecast": False},
        )
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != 1:
            raise PlantAPIError("Airtable did not return the updated record", status_code=502)
        return records[0]

    def _request(
        self,
        method: str,
        table_id: str,
        *,
        query: list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            raise PlantAPIError("Airtable write token is not configured", status_code=503)
        encoded_query = f"?{urlencode(query)}" if query else ""
        url = (
            f"{AIRTABLE_API_ROOT}/{quote(self.base_id, safe='')}/"
            f"{quote(table_id, safe='')}{encoded_query}"
        )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(5):
            delay = 0.22 - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            request = Request(
                url,
                method=method,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "plant-diary-gpt-action/0.1",
                },
            )
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise PlantAPIError("Airtable returned invalid JSON", status_code=502)
                return payload
            except HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 4:
                    time.sleep(min(2**attempt, 15))
                    continue
                try:
                    detail = exc.read(1024).decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                raise PlantAPIError(
                    f"Airtable request failed with HTTP {exc.code}: {detail[:300]}",
                    status_code=502,
                    code="airtable_error",
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 4:
                    time.sleep(min(2**attempt, 15))
                    continue
                raise PlantAPIError(
                    f"Airtable request failed: {exc}", status_code=502, code="airtable_error"
                ) from exc
        raise AssertionError("Airtable retry loop ended unexpectedly")


class AirtableRepository:
    def __init__(self, settings: Settings, client: AirtableHTTPClient | None = None) -> None:
        self.settings = settings
        self.client = client or AirtableHTTPClient(settings.airtable_token, settings.airtable_base_id)

    def plants(self) -> list[PlantRecord]:
        records = self.client.list_records(self.settings.plants_table_id, PLANT_FIELDS.values())
        result: list[PlantRecord] = []
        for record in records:
            fields = record.get("fields", {})
            record_id = _record_id(record.get("id"), "plant")
            plant_id = _integer(fields.get(PLANT_FIELDS["plant_id"]), f"plant {record_id}")
            name = _text(fields.get(PLANT_FIELDS["name"]))
            if not name:
                raise PlantAPIError(f"plant {record_id} has an empty name", status_code=502)
            result.append(
                PlantRecord(
                    record_id=record_id,
                    plant_id=plant_id,
                    name=name,
                    latin_name=_text(fields.get(PLANT_FIELDS["latin_name"])),
                    cultivar=_text(fields.get(PLANT_FIELDS["cultivar"])),
                    group=_text(fields.get(PLANT_FIELDS["group"])),
                    status=_text(fields.get(PLANT_FIELDS["status"])),
                    notes=_text(fields.get(PLANT_FIELDS["notes"])),
                    date_added=_text(fields.get(PLANT_FIELDS["date_added"])) or None,
                    main_photo_url=_text(fields.get(PLANT_FIELDS["main_photo_url"])) or None,
                )
            )
        seen: set[int] = set()
        duplicates: set[int] = set()
        for plant in result:
            if plant.plant_id in seen:
                duplicates.add(plant.plant_id)
            seen.add(plant.plant_id)
        if duplicates:
            values = ", ".join(f"#{value}" for value in sorted(duplicates))
            raise PlantAPIError(
                f"Airtable contains duplicate Plant IDs: {values}",
                status_code=409,
                code="duplicate_plant_id",
            )
        return sorted(result, key=lambda item: item.plant_id)

    def plant_by_id(self, plant_id: int) -> PlantRecord:
        for plant in self.plants():
            if plant.plant_id == plant_id:
                return plant
        raise PlantAPIError(f"Plant ID #{plant_id} was not found", status_code=404, code="not_found")

    def photos(self, plants_by_record_id: dict[str, int] | None = None) -> list[PhotoRecord]:
        if plants_by_record_id is None:
            plants_by_record_id = {item.record_id: item.plant_id for item in self.plants()}
        records = self.client.list_records(self.settings.photos_table_id, PHOTO_FIELDS.values())
        result: list[PhotoRecord] = []
        for record in records:
            fields = record.get("fields", {})
            record_id = _record_id(record.get("id"), "photo")
            relations = fields.get(PHOTO_FIELDS["plants_relation"]) or []
            if not isinstance(relations, list):
                relations = []
            relation_ids = tuple(str(value) for value in relations)
            unknown_relations = sorted(
                value for value in relation_ids if value not in plants_by_record_id
            )
            if unknown_relations:
                raise PlantAPIError(
                    f"Photo {record_id} links unknown plant records: "
                    f"{', '.join(unknown_relations)}",
                    status_code=502,
                    code="broken_relation",
                )
            plant_ids = tuple(
                sorted({plants_by_record_id[value] for value in relation_ids if value in plants_by_record_id})
            )
            raw_scalar_id = fields.get(PHOTO_FIELDS["plant_id"])
            if raw_scalar_id not in (None, ""):
                scalar_id = _integer(raw_scalar_id, f"photo {record_id}")
                if scalar_id not in plant_ids:
                    raise PlantAPIError(
                        f"Photo {record_id} has Plant ID #{scalar_id} but its relation does not",
                        status_code=502,
                        code="broken_relation",
                    )
            result.append(
                PhotoRecord(
                    record_id=record_id,
                    filename=_text(fields.get(PHOTO_FIELDS["filename"])),
                    source_file_id=_text(fields.get(PHOTO_FIELDS["source_file_id"])),
                    plant_ids=plant_ids,
                    plant_record_ids=relation_ids,
                    date=_text(fields.get(PHOTO_FIELDS["date"])) or None,
                    photo_type=_text(fields.get(PHOTO_FIELDS["type"])),
                    comment=_text(fields.get(PHOTO_FIELDS["comment"])),
                    web_key=_text(fields.get(PHOTO_FIELDS["web_key"])),
                    url=_text(fields.get(PHOTO_FIELDS["url"])),
                    upload_status=_text(fields.get(PHOTO_FIELDS["upload_status"])),
                )
            )
        return result

    def create_plant(self, fields: dict[str, Any]) -> str:
        record = self.client.create_record(self.settings.plants_table_id, fields)
        return _record_id(record.get("id"), "created plant")

    def update_plant(self, record_id: str, fields: dict[str, Any]) -> None:
        self.client.update_record(self.settings.plants_table_id, record_id, fields)

    def create_photo(self, fields: dict[str, Any]) -> str:
        record = self.client.create_record(self.settings.photos_table_id, fields)
        return _record_id(record.get("id"), "created photo")

    def create_event(self, fields: dict[str, Any]) -> str:
        record = self.client.create_record(self.settings.events_table_id, fields)
        return _record_id(record.get("id"), "created event")
