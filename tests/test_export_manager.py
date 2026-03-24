import json
import os
import tempfile
import unittest

from config import hardware as config
from core.export_manager import ExportManager


class ExportManagerTests(unittest.TestCase):
    def test_register_export_keeps_pending_when_usb_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ExportManager(temp_dir, usb_roots_provider=lambda: [])
            local_pdf = os.path.join(manager.ensure_results_dir(), "calibracion_001.pdf")
            with open(local_pdf, "wb") as f:
                f.write(b"pdf-test")

            result = manager.register_export(local_pdf)

            self.assertFalse(result.usb_detected)
            self.assertEqual(result.pending_count, 1)
            self.assertEqual(result.copied_count, 0)

            with open(manager.queue_file, "r", encoding="utf-8") as f:
                queue_data = json.load(f)
            self.assertEqual(len(queue_data), 1)
            self.assertEqual(os.path.abspath(queue_data[0]["local_path"]), os.path.abspath(local_pdf))

    def test_pending_files_copy_when_usb_appears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            usb_roots: list[str] = []
            manager = ExportManager(temp_dir, usb_roots_provider=lambda: list(usb_roots))
            local_pdf = os.path.join(manager.ensure_results_dir(), "calibracion_002.pdf")
            with open(local_pdf, "wb") as f:
                f.write(b"pdf-test")

            first_result = manager.register_export(local_pdf)
            self.assertEqual(first_result.pending_count, 1)

            usb_root = os.path.join(temp_dir, "usb_montada")
            os.makedirs(usb_root, exist_ok=True)
            usb_roots.append(usb_root)

            sync_result = manager.sync_pending_exports(preferred_local_path=local_pdf)
            copied_pdf = os.path.join(usb_root, str(config.USB_EXPORT_DIRNAME), os.path.basename(local_pdf))

            self.assertTrue(sync_result.usb_detected)
            self.assertEqual(sync_result.copied_count, 1)
            self.assertEqual(sync_result.pending_count, 0)
            self.assertEqual(os.path.abspath(sync_result.preferred_usb_path), os.path.abspath(copied_pdf))
            self.assertTrue(os.path.isfile(copied_pdf))

            with open(manager.queue_file, "r", encoding="utf-8") as f:
                queue_data = json.load(f)
            self.assertEqual(queue_data, [])

    def test_register_export_does_not_duplicate_pending_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ExportManager(temp_dir, usb_roots_provider=lambda: [])
            local_pdf = os.path.join(manager.ensure_results_dir(), "calibracion_003.pdf")
            with open(local_pdf, "wb") as f:
                f.write(b"pdf-test")

            first_result = manager.register_export(local_pdf)
            second_result = manager.register_export(local_pdf)

            self.assertEqual(first_result.pending_count, 1)
            self.assertEqual(second_result.pending_count, 1)

            with open(manager.queue_file, "r", encoding="utf-8") as f:
                queue_data = json.load(f)
            self.assertEqual(len(queue_data), 1)


if __name__ == "__main__":
    unittest.main()
