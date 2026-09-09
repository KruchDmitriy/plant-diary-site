from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


PHOTO_TYPES = {
    "Основное",
    "Состояние",
    "Корни",
    "Цветение",
    "Историческое",
    "Референс продавца",
}

EVENT_TYPES = {
    "Наблюдение",
    "Пересадка",
    "Обрезка",
    "Цветение",
    "Полив",
    "Обработка",
    "Размножение",
    "Покупка/получение",
    "Продажа/передача",
    "Другое",
}

DATE_PRECISIONS = {"Точная", "Приблизительная", "Только месяц/год", "Неизвестна"}

ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}


class PlantAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class OpenAIFileRef:
    name: str
    file_id: str
    mime_type: str
    download_link: str

    @classmethod
    def from_value(cls, value: Any) -> "OpenAIFileRef":
        if not isinstance(value, dict):
            raise PlantAPIError("Each openaiFileIdRefs item must be a file reference object")
        name = str(value.get("name") or "").strip()
        file_id = str(value.get("id") or "").strip()
        mime_type = str(value.get("mime_type") or "").strip().lower()
        download_link = str(value.get("download_link") or "").strip()
        if not name or not file_id or not mime_type or not download_link:
            raise PlantAPIError("File reference is missing name, id, mime_type, or download_link")
        if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise PlantAPIError(f"Unsupported image type: {mime_type}")
        parsed = urlparse(download_link)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host.endswith(".oaiusercontent.com"):
            raise PlantAPIError("File download URL must be an HTTPS oaiusercontent.com URL")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", file_id):
            raise PlantAPIError("Invalid OpenAI file ID")
        return cls(name=name, file_id=file_id, mime_type=mime_type, download_link=download_link)


def parse_files(values: Any, *, max_files: int) -> list[OpenAIFileRef]:
    if not isinstance(values, list) or not values:
        raise PlantAPIError("Attach at least one image")
    if len(values) > max_files:
        raise PlantAPIError(f"At most {max_files} images can be sent in one request")
    result = [OpenAIFileRef.from_value(value) for value in values]
    ids = [item.file_id for item in result]
    if len(ids) != len(set(ids)):
        raise PlantAPIError("The same file was attached more than once")
    return result


def parse_iso_date(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise PlantAPIError(f"{field_name} must use YYYY-MM-DD") from exc
    return raw


def require_confirmation(value: Any) -> None:
    if value is not True:
        raise PlantAPIError(
            "Explicit user confirmation is required before changing the diary",
            status_code=409,
            code="confirmation_required",
        )


def validate_photo_type(value: Any) -> str:
    result = str(value or "Состояние").strip()
    if result not in PHOTO_TYPES:
        raise PlantAPIError(f"Unknown photo type: {result}")
    return result


def validate_event_type(value: Any) -> str:
    result = str(value or "").strip()
    if result not in EVENT_TYPES:
        raise PlantAPIError(f"Unknown event type: {result}")
    return result


def validate_date_precision(value: Any) -> str:
    result = str(value or "").strip()
    if result not in DATE_PRECISIONS:
        raise PlantAPIError(f"Unknown date precision: {result}")
    return result


def public_image_url(root: str, web_key: str) -> str:
    safe_chars = "/!$&'()*+,;=:@-._~"
    return f"{root.rstrip('/')}/{quote(web_key, safe=safe_chars)}"


def load_protected_names(path: Path) -> dict[int, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlantAPIError(f"Cannot read sync baseline: {exc}", status_code=500) from exc
    result: dict[int, str] = {}
    for raw_id, name in payload.get("protected_plant_names", {}).items():
        try:
            result[int(raw_id)] = str(name)
        except (TypeError, ValueError):
            continue
    return result


def rename_approval_sentence(old_name: str, new_name: str) -> str:
    return f"Название уточнено: «{old_name}» → «{new_name}»."
