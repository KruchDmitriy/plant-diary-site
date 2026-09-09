import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "sync_airtable.py"
SPEC = importlib.util.spec_from_file_location("sync_airtable", SCRIPT_PATH)
assert SPEC and SPEC.loader
sync_airtable = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync_airtable
SPEC.loader.exec_module(sync_airtable)


def record(record_id, fields):
    return {"id": record_id, "fields": fields}


class NormalizeSnapshotTests(unittest.TestCase):
    def setUp(self):
        plant_fields = sync_airtable.PLANT_FIELDS
        photo_fields = sync_airtable.PHOTO_FIELDS
        event_fields = sync_airtable.EVENT_FIELDS

        self.plants = [
            record(
                "recPlant1",
                {
                    plant_fields["name"][0]: "Plant one",
                    plant_fields["plant_id"][0]: 1,
                    plant_fields["group"][0]: "Group",
                    plant_fields["status"][0]: "Active",
                },
            ),
            record(
                "recPlant2",
                {
                    plant_fields["name"][0]: "Plant two",
                    plant_fields["plant_id"][0]: 2,
                    plant_fields["group"][0]: "Group",
                    plant_fields["status"][0]: "Active",
                },
            ),
        ]
        self.photos = [
            record(
                "recPhoto",
                {
                    photo_fields["filename"][0]: "group.jpeg",
                    photo_fields["date"][0]: "2026-09-01",
                    photo_fields["object_key"][0]: "originals/2026-09-01/group.jpeg",
                    photo_fields["type"][0]: "Состояние",
                    photo_fields["plants_relation"][0]: ["recPlant1", "recPlant2"],
                    photo_fields["web_key"][0]: "web/2026-09-01/group.webp",
                    photo_fields["sha256"][0]: "a" * 64,
                    photo_fields["upload_status"][0]: "Загружено",
                },
            )
        ]
        self.events = [
            record(
                "recEvent",
                {
                    event_fields["title"][0]: "Observed",
                    event_fields["plant_id"][0]: 1,
                    event_fields["date"][0]: "2026-09-02",
                    event_fields["type"][0]: "Наблюдение",
                    event_fields["description"][0]: "Looks healthy",
                    event_fields["plant_relation"][0]: ["recPlant1"],
                    event_fields["date_precision"][0]: "Точная",
                },
            )
        ]

    def test_preserves_multi_plant_photo_relations(self):
        plants, photos, events = sync_airtable.normalize_snapshot(
            self.plants, self.photos, self.events
        )
        self.assertEqual(photos[0]["plant_ids"], [1, 2])
        self.assertEqual([plant["photo_count"] for plant in plants], [1, 1])
        self.assertEqual([plant["event_count"] for plant in plants], [1, 0])
        self.assertEqual(events[0]["plant_id"], 1)

    def test_preserves_legacy_photo_index_and_dimensions(self):
        plants, photos, events = sync_airtable.normalize_snapshot(
            self.plants,
            self.photos,
            self.events,
            existing_photos=[
                {
                    "index": 42,
                    "web_key": "web/2026-09-01/group.webp",
                    "width": 1200,
                    "height": 900,
                }
            ],
        )
        self.assertEqual(photos[0]["index"], 42)
        self.assertEqual(photos[0]["width"], 1200)
        self.assertEqual(photos[0]["height"], 900)

    def test_rejects_scalar_photo_id_without_relation(self):
        fields = self.photos[0]["fields"]
        fields[sync_airtable.PHOTO_FIELDS["plants_relation"][0]] = []
        fields[sync_airtable.PHOTO_FIELDS["plant_id"][0]] = 1
        with self.assertRaisesRegex(sync_airtable.SyncError, "no plant relation"):
            sync_airtable.normalize_snapshot(self.plants, self.photos, self.events)

    def test_rejects_event_relation_mismatch(self):
        fields = self.events[0]["fields"]
        fields[sync_airtable.EVENT_FIELDS["plant_id"][0]] = 2
        with self.assertRaisesRegex(sync_airtable.SyncError, "contradicts linked plant"):
            sync_airtable.normalize_snapshot(self.plants, self.photos, self.events)


if __name__ == "__main__":
    unittest.main()
