import hashlib
import io
import unittest

from PIL import Image

from plant_api.domain import OpenAIFileRef, PlantAPIError
from plant_api.media import DownloadedImage, prepare_image


class PlantAPIMediaTests(unittest.TestCase):
    @staticmethod
    def png_source(mime_type="image/png"):
        payload = io.BytesIO()
        Image.new("RGB", (2000, 1000), (32, 128, 64)).save(payload, format="PNG")
        data = payload.getvalue()
        ref = OpenAIFileRef(
            name="Моё растение.png",
            file_id="file-test_123",
            mime_type=mime_type,
            download_link="https://files.oaiusercontent.com/file-test_123",
        )
        return DownloadedImage(ref=ref, data=data, sha256=hashlib.sha256(data).hexdigest())

    def test_generates_bounded_webp_and_collision_resistant_keys(self):
        result = prepare_image(self.png_source(), date_path="2026-09-09")
        self.assertEqual((result.width, result.height), (1600, 800))
        self.assertEqual(
            result.original_key,
            "originals/2026-09-09/file-test_123_photo.png",
        )
        self.assertEqual(
            result.web_key,
            "web/2026-09-09/file-test_123_photo.webp",
        )
        with Image.open(io.BytesIO(result.webp)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.getexif(), {})

    def test_rejects_mime_type_that_does_not_match_content(self):
        with self.assertRaisesRegex(PlantAPIError, "does not match"):
            prepare_image(self.png_source("image/jpeg"), date_path="undated")


if __name__ == "__main__":
    unittest.main()
