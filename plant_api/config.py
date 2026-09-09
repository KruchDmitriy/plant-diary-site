from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    api_key: str
    write_enabled: bool
    airtable_token: str
    airtable_base_id: str = "appnyj7AhSHhW4PUe"
    plants_table_id: str = "tbljZlrQNMTpq4E2g"
    photos_table_id: str = "tblswsaSHsGjBp8E9"
    events_table_id: str = "tblqFSe8BKHSl8yp1"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_bucket: str = "plant-diary-photos"
    storage_endpoint: str = "https://storage.yandexcloud.net"
    storage_region: str = "ru-central1"
    public_image_root: str = "https://storage.yandexcloud.net/plant-diary-photos"
    github_token: str = ""
    github_repository: str = "KruchDmitriy/plant-diary-site"
    github_workflow: str = "deploy-pages.yml"
    public_api_url: str = ""
    max_files: int = 10
    max_file_bytes: int = 25 * 1024 * 1024
    baseline_path: Path = PROJECT_ROOT / "data" / "sync_baseline.json"
    snapshot_path: Path = PROJECT_ROOT / "data" / "plants.json"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("PLANT_API_KEY", ""),
            write_enabled=_bool_env("PLANT_API_WRITE_ENABLED"),
            airtable_token=os.getenv("AIRTABLE_WRITE_TOKEN", ""),
            airtable_base_id=os.getenv("AIRTABLE_BASE_ID", cls.airtable_base_id),
            plants_table_id=os.getenv("AIRTABLE_PLANTS_TABLE_ID", cls.plants_table_id),
            photos_table_id=os.getenv("AIRTABLE_PHOTOS_TABLE_ID", cls.photos_table_id),
            events_table_id=os.getenv("AIRTABLE_EVENTS_TABLE_ID", cls.events_table_id),
            storage_access_key=os.getenv("AWS_ACCESS_KEY_ID", ""),
            storage_secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            storage_bucket=os.getenv("S3_BUCKET", cls.storage_bucket),
            storage_endpoint=os.getenv("S3_ENDPOINT", cls.storage_endpoint),
            storage_region=os.getenv("S3_REGION", cls.storage_region),
            public_image_root=os.getenv("PUBLIC_IMAGE_ROOT", cls.public_image_root).rstrip("/"),
            github_token=os.getenv("PLANT_GITHUB_TOKEN", ""),
            github_repository=os.getenv("GITHUB_REPOSITORY", cls.github_repository),
            github_workflow=os.getenv("GITHUB_WORKFLOW", cls.github_workflow),
            public_api_url=os.getenv("PLANT_API_PUBLIC_URL", "").rstrip("/"),
            max_files=_int_env("PLANT_API_MAX_FILES", 10),
            max_file_bytes=_int_env("PLANT_API_MAX_FILE_BYTES", 25 * 1024 * 1024),
            baseline_path=Path(
                os.getenv("PLANT_SYNC_BASELINE", str(PROJECT_ROOT / "data" / "sync_baseline.json"))
            ),
            snapshot_path=Path(
                os.getenv("PLANT_SNAPSHOT", str(PROJECT_ROOT / "data" / "plants.json"))
            ),
        )

    def missing_write_secrets(self) -> list[str]:
        required = {
            "PLANT_API_KEY": self.api_key,
            "AIRTABLE_WRITE_TOKEN": self.airtable_token,
            "AWS_ACCESS_KEY_ID": self.storage_access_key,
            "AWS_SECRET_ACCESS_KEY": self.storage_secret_key,
        }
        return [name for name, value in required.items() if not value]
