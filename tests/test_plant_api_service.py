import tempfile
import unittest
from pathlib import Path

from plant_api.airtable import EVENT_FIELDS, PLANT_FIELDS, PhotoRecord, PlantRecord
from plant_api.config import Settings
from plant_api.domain import PlantAPIError, rename_approval_sentence
from plant_api.service import PlantDiaryService


class FakeRepository:
    def __init__(self):
        self._plants = [
            PlantRecord(
                record_id="recPlant1",
                plant_id=1,
                name="Old name",
                latin_name="",
                cultivar="",
                group="Succulents",
                status="Active",
                notes="",
                date_added=None,
                main_photo_url=None,
            ),
            PlantRecord(
                record_id="recPlant128",
                plant_id=128,
                name="Суккулент 128",
                latin_name="",
                cultivar="",
                group="Суккуленты",
                status="Наблюдение",
                notes="",
                date_added=None,
                main_photo_url=None,
            ),
        ]
        self._photos = [
            PhotoRecord(
                record_id="recPhoto1",
                filename="one.jpg",
                source_file_id="file-one",
                plant_ids=(128,),
                plant_record_ids=("recPlant128",),
                date="2026-09-09",
                photo_type="Состояние",
                comment="",
                web_key="web/2026-09-09/one.webp",
                url="https://storage.example/web/2026-09-09/one.webp",
                upload_status="Загружено",
            ),
            PhotoRecord(
                record_id="recGroupPhoto",
                filename="tray.jpg",
                source_file_id="file-tray",
                plant_ids=(1, 128),
                plant_record_ids=("recPlant1", "recPlant128"),
                date="2026-09-09",
                photo_type="Состояние",
                comment="",
                web_key="web/2026-09-09/tray.webp",
                url="https://storage.example/web/2026-09-09/tray.webp",
                upload_status="Загружено",
            ),
        ]
        self.updated = []
        self.events = []
        self.created_plants = []

    def plants(self):
        return list(self._plants)

    def plant_by_id(self, plant_id):
        for plant in self._plants:
            if plant.plant_id == plant_id:
                return plant
        raise PlantAPIError("not found", status_code=404)

    def photos(self, plants_by_record_id=None):
        return list(self._photos)

    def update_plant(self, record_id, fields):
        self.updated.append((record_id, fields))

    def create_plant(self, fields):
        record_id = f"recPlant{fields[PLANT_FIELDS['plant_id']]}"
        self.created_plants.append((record_id, fields))
        self._plants.append(
            PlantRecord(
                record_id=record_id,
                plant_id=fields[PLANT_FIELDS["plant_id"]],
                name=fields[PLANT_FIELDS["name"]],
                latin_name=fields.get(PLANT_FIELDS["latin_name"], ""),
                cultivar=fields.get(PLANT_FIELDS["cultivar"], ""),
                group=fields.get(PLANT_FIELDS["group"], ""),
                status=fields.get(PLANT_FIELDS["status"], ""),
                notes=fields.get(PLANT_FIELDS["notes"], ""),
                date_added=fields.get(PLANT_FIELDS["date_added"]),
                main_photo_url=None,
            )
        )
        return record_id

    def create_event(self, fields):
        self.events.append(fields)
        return "recEvent"


class FakeDeployer:
    def __init__(self):
        self.calls = 0

    def trigger(self):
        self.calls += 1
        return "triggered"


class PlantDiaryServiceTests(unittest.TestCase):
    def make_service(self, *, write_enabled=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        baseline = Path(temp.name) / "baseline.json"
        baseline.write_text(
            '{"protected_plant_names":{"1":"Old name"}}', encoding="utf-8"
        )
        settings = Settings(
            api_key="api-key",
            write_enabled=write_enabled,
            airtable_token="token" if write_enabled else "",
            storage_access_key="access" if write_enabled else "",
            storage_secret_key="secret" if write_enabled else "",
            public_image_root="https://storage.example",
            baseline_path=baseline,
        )
        repository = FakeRepository()
        deployer = FakeDeployer()
        service = PlantDiaryService(
            settings,
            repository=repository,
            storage=object(),
            deployer=deployer,
        )
        return service, repository, deployer

    def test_searches_by_id_and_generic_name(self):
        service, _, _ = self.make_service()
        self.assertEqual(service.search_plants("#128")["plants"][0]["plant_id"], 128)
        self.assertEqual(service.search_plants("суккулент")["plants"][0]["plant_id"], 128)

    def test_get_plant_uses_same_automatic_cover_rule_as_site(self):
        service, repository, _ = self.make_service()
        repository._photos.append(
            PhotoRecord(
                record_id="recMainPhoto",
                filename="main.jpg",
                source_file_id="file-main",
                plant_ids=(128,),
                plant_record_ids=("recPlant128",),
                date="2026-09-01",
                photo_type="Основное",
                comment="",
                web_key="web/2026-09-01/main.webp",
                url="",
                upload_status="Загружено",
            )
        )

        result = service.get_plant(128)

        self.assertEqual(
            result["main_photo_url"],
            "https://storage.example/web/2026-09-01/main.webp",
        )

    def test_rename_requires_explicit_confirmation(self):
        service, repository, _ = self.make_service()
        result = service.update_identity(
            128,
            {
                "expected_current_name": "Суккулент 128",
                "name": "Haworthia cooperi",
                "confirmed": False,
            },
        )
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(repository.updated, [])

    def test_confirmed_mutation_stays_dry_until_write_enabled(self):
        service, repository, _ = self.make_service()
        result = service.update_identity(
            128,
            {
                "expected_current_name": "Суккулент 128",
                "name": "Haworthia cooperi",
                "confirmed": True,
            },
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(repository.updated, [])

    def test_protected_rename_creates_exact_approval_event(self):
        service, repository, deployer = self.make_service(write_enabled=True)
        result = service.update_identity(
            1,
            {
                "expected_current_name": "Old name",
                "name": "Corrected name",
                "confirmed": True,
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(deployer.calls, 1)
        self.assertEqual(
            repository.events[0][EVENT_FIELDS["description"]],
            rename_approval_sentence("Old name", "Corrected name"),
        )
        self.assertEqual(
            repository.updated[0],
            ("recPlant1", {PLANT_FIELDS["name"]: "Corrected name"}),
        )

    def test_cover_must_belong_to_plant(self):
        service, _, _ = self.make_service()
        with self.assertRaisesRegex(PlantAPIError, "Choose exactly one"):
            service.set_cover(
                1,
                {"photo_record_id": "recPhoto1", "confirmed": True},
            )

    def test_group_cover_needs_second_confirmation(self):
        service, _, _ = self.make_service()
        result = service.set_cover(
            128,
            {"photo_record_id": "recGroupPhoto", "confirmed": True},
        )
        self.assertEqual(result["status"], "group_photo_confirmation_required")
        self.assertTrue(result["planned_change"]["is_group_photo"])

    def test_sets_existing_single_plant_cover(self):
        service, repository, deployer = self.make_service(write_enabled=True)
        result = service.set_cover(
            128,
            {"photo_record_id": "recPhoto1", "confirmed": True},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(deployer.calls, 1)
        self.assertEqual(
            repository.updated[0],
            (
                "recPlant128",
                {PLANT_FIELDS["main_photo_url"]: "https://storage.example/web/2026-09-09/one.webp"},
            ),
        )

    def test_cover_selector_must_be_unambiguous(self):
        service, _, _ = self.make_service()
        with self.assertRaisesRegex(PlantAPIError, "exactly one"):
            service.set_cover(
                128,
                {
                    "photo_record_id": "recPhoto1",
                    "web_key": "web/2026-09-09/one.webp",
                    "confirmed": True,
                },
            )

    def test_existing_uploaded_file_can_be_reused_as_cover_without_duplicate(self):
        service, repository, _ = self.make_service(write_enabled=True)
        result = service.add_photos(
            128,
            {
                "openaiFileIdRefs": [
                    {
                        "name": "one.jpg",
                        "id": "file-one",
                        "mime_type": "image/jpeg",
                        "download_link": "https://files.oaiusercontent.com/file-one",
                    }
                ],
                "type": "Состояние",
                "cover_file_id": "file-one",
                "confirmed": True,
            },
        )
        self.assertEqual(result["photos"][0]["status"], "already_present")
        self.assertTrue(result["cover_updated"])
        self.assertEqual(
            repository.updated[-1],
            (
                "recPlant128",
                {PLANT_FIELDS["main_photo_url"]: "https://storage.example/web/2026-09-09/one.webp"},
            ),
        )

    def test_new_plant_requires_confirmed_proposed_id(self):
        service, repository, _ = self.make_service(write_enabled=True)
        with self.assertRaisesRegex(PlantAPIError, "expected_plant_id"):
            service.create_plant({"name": "New plant", "confirmed": True})
        self.assertEqual(repository.created_plants, [])

    def test_new_plant_rejects_stale_proposed_id(self):
        service, repository, _ = self.make_service(write_enabled=True)
        with self.assertRaisesRegex(PlantAPIError, "current next ID is #129"):
            service.create_plant(
                {"name": "New plant", "expected_plant_id": 130, "confirmed": True}
            )
        self.assertEqual(repository.created_plants, [])

    def test_new_plant_validates_acquisition_event_before_writing(self):
        service, repository, _ = self.make_service(write_enabled=True)
        with self.assertRaisesRegex(PlantAPIError, "either an exact date"):
            service.create_plant(
                {
                    "name": "New plant",
                    "expected_plant_id": 129,
                    "confirmed": True,
                    "acquisition_event": {
                        "type": "Покупка/получение",
                        "date_precision": "Неизвестна",
                    },
                }
            )
        self.assertEqual(repository.created_plants, [])

    def test_new_plant_keeps_existing_ids_and_rejects_retry(self):
        service, repository, _ = self.make_service(write_enabled=True)
        preview = service.create_plant({"name": "New plant", "confirmed": False})
        self.assertEqual(preview["planned_change"]["proposed_plant_id"], 129)
        result = service.create_plant(
            {"name": "New plant", "expected_plant_id": 129, "confirmed": True}
        )
        self.assertEqual(result["plant"]["plant_id"], 129)
        self.assertEqual([plant.plant_id for plant in repository._plants], [1, 128, 129])
        with self.assertRaisesRegex(PlantAPIError, "current next ID is #130"):
            service.create_plant(
                {"name": "New plant", "expected_plant_id": 129, "confirmed": True}
            )


if __name__ == "__main__":
    unittest.main()
