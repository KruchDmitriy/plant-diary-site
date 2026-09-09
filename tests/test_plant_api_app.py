import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from plant_api.app import create_app
from plant_api.config import Settings


class PlantAPIContractTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(api_key="test-key", write_enabled=False, airtable_token="")
        self.client = TestClient(create_app(settings))
        self.headers = {"X-Plant-API-Key": "test-key"}

    def test_auth_read_and_dry_run(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/plants/128").status_code, 401)
        current = self.client.get("/plants/128", headers=self.headers)
        self.assertEqual(current.status_code, 200)
        response = self.client.patch(
            "/plants/128/identity",
            headers=self.headers,
            json={
                "expected_current_name": current.json()["name"],
                "name": "Test only",
                "confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "dry_run")

    def test_client_cannot_supply_a_plant_id_field(self):
        response = self.client.post(
            "/plants",
            headers=self.headers,
            json={"plant_id": 999, "name": "Test", "confirmed": False},
        )
        self.assertEqual(response.status_code, 422)

    def test_custom_action_schema_has_expected_operations_and_file_shape(self):
        schema = yaml.safe_load(
            Path("plant_api/openapi-action.yaml").read_text(encoding="utf-8")
        )
        operation_ids = {
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        self.assertEqual(
            operation_ids,
            {
                "searchPlants",
                "getPlant",
                "listPlantPhotos",
                "addPhotos",
                "createPlant",
                "updatePlantIdentity",
                "setPlantCover",
                "clearPlantCover",
                "addPlantEvent",
            },
        )
        file_items = schema["paths"]["/plants/{plant_id}/photos"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"]["properties"][
            "openaiFileIdRefs"
        ]["items"]
        self.assertEqual(file_items, {"type": "string"})

    def test_custom_action_schema_uses_validator_compatible_inline_objects(self):
        schema = yaml.safe_load(
            Path("plant_api/openapi-action.yaml").read_text(encoding="utf-8")
        )
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                with self.subTest(path=path, method=method):
                    self.assertLessEqual(len(operation.get("description", "")), 300)
                    for parameter in operation.get("parameters", []):
                        self.assertIsInstance(parameter.get("name"), str)
                    request_body = operation.get("requestBody")
                    if request_body:
                        request_schema = request_body["content"]["application/json"]["schema"]
                        self.assertEqual(request_schema.get("type"), "object")
                        self.assertIsInstance(request_schema.get("properties"), dict)
                    for response in operation["responses"].values():
                        response_schema = response["content"]["application/json"]["schema"]
                        self.assertEqual(response_schema.get("type"), "object")
                        self.assertIsInstance(response_schema.get("properties"), dict)

    def test_served_action_schema_uses_configured_public_url(self):
        settings = Settings(
            api_key="test-key",
            write_enabled=False,
            airtable_token="",
            public_api_url="https://plants-api.example.net",
        )
        client = TestClient(create_app(settings))
        response = client.get("/openapi-action.yaml")
        self.assertEqual(response.status_code, 200)
        self.assertIn("url: https://plants-api.example.net", response.text)
        self.assertNotIn("plant-api.example.com", response.text)


if __name__ == "__main__":
    unittest.main()
