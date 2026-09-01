import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "migrate_media_layout.py"
SPEC = importlib.util.spec_from_file_location("migrate_media_layout", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MediaMigrationTests(unittest.TestCase):
    def test_code_from_video_and_sidecar_names(self):
        self.assertEqual(MODULE.code_from_name("ipx850.mp4"), "IPX-850")
        self.assertEqual(
            MODULE.code_from_name("FC2-PPV-4968748-poster.jpg"),
            "FC2-PPV-4968748",
        )
        self.assertIsNone(MODULE.code_from_name("readme.txt"))

    def test_plan_and_apply_groups_root_and_classified_flat_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "media"
            jav = media / "JAV"
            fc2 = media / "FC2"
            jav.mkdir(parents=True)
            fc2.mkdir()
            (media / "IPX-850.mp4").write_bytes(b"jav")
            (fc2 / "FC2-PPV-4968748.mp4").write_bytes(b"fc2")
            (fc2 / "FC2-PPV-4968748.nfo").write_text("nfo", encoding="utf-8")
            (media / "notes.txt").write_text("keep", encoding="utf-8")
            config = {
                "media_dir": str(media),
                "jav_media_dir": str(jav),
                "fc2_media_dir": str(fc2),
            }

            moves, skipped = MODULE.plan_moves(config)
            self.assertEqual(len(moves), 3)
            self.assertEqual(skipped, [media / "notes.txt"])
            MODULE.apply_moves(moves)

            self.assertTrue((jav / "IPX-850" / "IPX-850.mp4").is_file())
            self.assertTrue(
                (fc2 / "FC2-PPV-4968748" / "FC2-PPV-4968748.mp4").is_file()
            )
            self.assertTrue(
                (fc2 / "FC2-PPV-4968748" / "FC2-PPV-4968748.nfo").is_file()
            )
            self.assertTrue((media / "notes.txt").is_file())

    def test_existing_target_blocks_entire_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "media"
            jav = media / "JAV"
            jav.mkdir(parents=True)
            source = jav / "IPX-850.mp4"
            source.write_bytes(b"source")
            target = jav / "IPX-850" / "IPX-850.mp4"
            target.parent.mkdir()
            target.write_bytes(b"target")
            moves, _ = MODULE.plan_moves({"media_dir": str(media)})

            with self.assertRaises(RuntimeError):
                MODULE.apply_moves(moves)
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(target.read_bytes(), b"target")


if __name__ == "__main__":
    unittest.main()
