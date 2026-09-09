import unittest

from plant_api.airtable import (
    PHOTO_FIELDS,
    PLANT_FIELDS,
    AirtableRepository,
    _integer,
)
from plant_api.config import Settings
from plant_api.domain import PlantAPIError


class FakeAirtableClient:
    def __init__(self, records_by_table):
        self.records_by_table = records_by_table

    def list_records(self, table_id, _field_ids):
        return self.records_by_table.get(table_id, [])


class AirtableIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(api_key="key", write_enabled=False, airtable_token="token")

    @staticmethod
    def plant(record_id, plant_id, name):
        return {
            "id": record_id,
            "fields": {
                PLANT_FIELDS["plant_id"]: plant_id,
                PLANT_FIELDS["name"]: name,
            },
        }

    def test_rejects_duplicate_plant_ids_from_airtable(self):
        client = FakeAirtableClient(
            {
                self.settings.plants_table_id: [
                    self.plant("recPlantOne", 1, "One"),
                    self.plant("recPlantTwo", 1, "Two"),
                ]
            }
        )
        repository = AirtableRepository(self.settings, client=client)
        with self.assertRaisesRegex(PlantAPIError, "duplicate Plant IDs"):
            repository.plants()

    def test_rejects_photo_relation_to_unknown_plant(self):
        client = FakeAirtableClient(
            {
                self.settings.plants_table_id: [self.plant("recPlantOne", 1, "One")],
                self.settings.photos_table_id: [
                    {
                        "id": "recPhotoOne",
                        "fields": {
                            PHOTO_FIELDS["plants_relation"]: ["recMissingPlant"],
                        },
                    }
                ],
            }
        )
        repository = AirtableRepository(self.settings, client=client)
        with self.assertRaisesRegex(PlantAPIError, "unknown plant records"):
            repository.photos()

    def test_plant_ids_must_be_positive(self):
        with self.assertRaisesRegex(PlantAPIError, "non-positive"):
            _integer(0, "test")


if __name__ == "__main__":
    unittest.main()
