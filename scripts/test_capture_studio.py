"""Offline checks: python -X utf8 -m unittest discover -s scripts -p test_capture_studio.py"""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import capture_studio
from rbx_studio import StudioMCP


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5ioAAAAASUVORK5CYII=")


def image_result(data=PNG, mime_type="image/png"):
    return {"content": [{"type": "image", "mimeType": mime_type,
                         "data": base64.b64encode(data).decode("ascii")}]}


class CaptureTests(unittest.TestCase):
    def test_raw_call_preserves_images_and_existing_call_returns_text(self):
        client = object.__new__(StudioMCP)
        client.timeout = 90
        result = image_result()
        result["content"].append({"type": "text", "text": "captured"})
        with patch.object(client, "_rpc", return_value=result):
            self.assertIs(client.call_result("screen_capture", {}), result)
            self.assertEqual(client.call("screen_capture", {}), "captured")

    def test_raw_call_still_reports_tool_errors(self):
        client = object.__new__(StudioMCP)
        client.timeout = 90
        with patch.object(client, "_rpc", return_value={"isError": True, "content": [
                {"type": "text", "text": "Studio unavailable"}]}):
            with self.assertRaisesRegex(RuntimeError, "Studio unavailable"):
                client.call_result("screen_capture", {})

    def test_saves_original_image_and_infers_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            path, mime_type, size = capture_studio.save_image(
                image_result(), str(Path(directory) / "nested" / "capture"), "test")
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(mime_type, "image/png")
            self.assertEqual(size, len(PNG))

    def test_wrong_extension_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "capture.jpg"
            with self.assertRaisesRegex(ValueError, "returned image/png"):
                capture_studio.save_image(image_result(), str(target), "test")
            self.assertFalse(target.exists())

    def test_missing_image_explains_tool_response(self):
        with self.assertRaisesRegex(ValueError, "No viewport available"):
            capture_studio.decode_image({"content": [
                {"type": "text", "text": "No viewport available"}]})

    def test_rejects_corrupt_image_payloads(self):
        result = image_result()
        result["content"][0]["data"] = "not base64!"
        with self.assertRaisesRegex(ValueError, "invalid base64"):
            capture_studio.decode_image(result)
        with self.assertRaisesRegex(ValueError, "does not match"):
            capture_studio.decode_image(image_result(b"plain text"))


if __name__ == "__main__":
    unittest.main()
