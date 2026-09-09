from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import Settings
from .domain import OpenAIFileRef, PlantAPIError


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

MIME_IMAGE_FORMATS = {
    "image/jpeg": {"JPEG"},
    "image/png": {"PNG"},
    "image/webp": {"WEBP"},
    "image/heic": {"HEIC", "HEIF"},
    "image/heif": {"HEIC", "HEIF"},
}


@dataclass(frozen=True)
class DownloadedImage:
    ref: OpenAIFileRef
    data: bytes
    sha256: str


@dataclass(frozen=True)
class PreparedImage:
    source: DownloadedImage
    webp: bytes
    width: int
    height: int
    original_key: str
    web_key: str


def _trusted_file_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host.endswith(".oaiusercontent.com")


def download_image(ref: OpenAIFileRef, *, max_bytes: int, timeout: float = 20.0) -> DownloadedImage:
    if not _trusted_file_url(ref.download_link):
        raise PlantAPIError("Untrusted file download URL")
    request = Request(
        ref.download_link,
        headers={"Accept": "image/*", "User-Agent": "plant-diary-gpt-action/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if not _trusted_file_url(response.geturl()):
                raise PlantAPIError("File download redirected to an untrusted host")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise PlantAPIError(f"{ref.name} is larger than the configured upload limit")
            data = response.read(max_bytes + 1)
    except PlantAPIError:
        raise
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise PlantAPIError(
            f"Could not download {ref.name}: {exc}", status_code=502, code="download_failed"
        ) from exc
    if len(data) > max_bytes:
        raise PlantAPIError(f"{ref.name} is larger than the configured upload limit")
    if not data:
        raise PlantAPIError(f"{ref.name} is empty")
    return DownloadedImage(ref=ref, data=data, sha256=hashlib.sha256(data).hexdigest())


def _slug(value: str) -> str:
    stem = Path(value).stem
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return (result or "photo")[:80]


def prepare_image(
    source: DownloadedImage,
    *,
    date_path: str,
    max_edge: int = 1600,
    quality: int = 82,
) -> PreparedImage:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass
    except ImportError as exc:
        raise PlantAPIError("Image processing dependencies are not installed", status_code=503) from exc

    Image.MAX_IMAGE_PIXELS = 50_000_000
    try:
        with Image.open(io.BytesIO(source.data)) as image:
            image_format = str(image.format or "").upper()
            if image_format not in MIME_IMAGE_FORMATS[source.ref.mime_type]:
                raise PlantAPIError(
                    f"{source.ref.name} content does not match {source.ref.mime_type}"
                )
            if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise PlantAPIError(
                    f"{source.ref.name} exceeds the 50-megapixel safety limit"
                )
            image = ImageOps.exif_transpose(image)
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise PlantAPIError(f"{source.ref.name} has invalid dimensions")
            if max(image.size) > max_edge:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            output = io.BytesIO()
            # EXIF is intentionally not passed: public WebP derivatives contain no phone metadata.
            image.save(output, format="WEBP", quality=quality, method=6)
            width, height = image.size
    except UnidentifiedImageError as exc:
        raise PlantAPIError(f"{source.ref.name} is not a supported image") from exc
    except PlantAPIError:
        raise
    except Exception as exc:
        raise PlantAPIError(f"Could not process {source.ref.name}: {exc}") from exc

    file_id = source.ref.file_id
    slug = _slug(source.ref.name)
    extension = MIME_EXTENSIONS[source.ref.mime_type]
    original_key = f"originals/{date_path}/{file_id}_{slug}{extension}"
    web_key = f"web/{date_path}/{file_id}_{slug}.webp"
    return PreparedImage(
        source=source,
        webp=output.getvalue(),
        width=width,
        height=height,
        original_key=original_key,
        web_key=web_key,
    )


class YandexStorage:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise PlantAPIError("boto3 is not installed", status_code=503) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.storage_endpoint,
                region_name=self.settings.storage_region,
                aws_access_key_id=self.settings.storage_access_key,
                aws_secret_access_key=self.settings.storage_secret_key,
            )
        return self._client

    def store(self, image: PreparedImage, *, plant_id: int) -> None:
        self._put_idempotent(
            key=image.original_key,
            data=image.source.data,
            content_type=image.source.ref.mime_type,
            sha256=image.source.sha256,
            metadata={
                "sha256": image.source.sha256,
                "plant-ids": str(plant_id),
                "source-file-id": image.source.ref.file_id,
                "kind": "original",
            },
            cache_control="private, max-age=31536000, immutable",
        )
        web_sha = hashlib.sha256(image.webp).hexdigest()
        self._put_idempotent(
            key=image.web_key,
            data=image.webp,
            content_type="image/webp",
            sha256=web_sha,
            metadata={
                "sha256": web_sha,
                "plant-ids": str(plant_id),
                "kind": "web",
            },
            cache_control="public, max-age=31536000, immutable",
        )

    def _put_idempotent(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        sha256: str,
        metadata: dict[str, str],
        cache_control: str,
    ) -> None:
        existing = None
        try:
            existing = self.client.head_object(Bucket=self.settings.storage_bucket, Key=key)
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(response.get("Error", {}).get("Code", ""))
            if status != 404 and code not in {"404", "NoSuchKey", "NotFound"}:
                raise PlantAPIError(
                    f"Could not inspect storage object {key}: {exc}",
                    status_code=502,
                    code="storage_error",
                ) from exc
        if existing is not None:
            existing_sha = existing.get("Metadata", {}).get("sha256")
            if existing_sha and existing_sha != sha256:
                raise PlantAPIError(
                    f"Storage key collision for {key}", status_code=409, code="storage_collision"
                )
            return
        try:
            self.client.put_object(
                Bucket=self.settings.storage_bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl=cache_control,
                Metadata=metadata,
            )
        except Exception as exc:
            raise PlantAPIError(
                f"Could not upload {key}: {exc}", status_code=502, code="storage_error"
            ) from exc
