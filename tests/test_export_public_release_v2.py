from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_public_release_v2 import (
    ContentPattern,
    ReleaseManifest,
    ReleaseSafetyError,
    apply_plan,
    build_plan,
    load_manifest,
)


class PublicReleaseV2Test(unittest.TestCase):
    def _roots(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory)
        source = root / "source"
        public = root / "public"
        source.mkdir()
        public.mkdir()
        return source, public

    def _manifest(self, **overrides: object) -> ReleaseManifest:
        values = {
            "include": ("app.py",),
            "preserve": ("README.md",),
            "deny": (".env", "**/.env"),
            "ignore": ("**/__pycache__/**",),
            "forbidden_content_patterns": (
                ContentPattern("test_token", r"TOKEN_[A-Z0-9]{8}"),
            ),
        }
        values.update(overrides)
        return ReleaseManifest(**values)

    def test_plan_classifies_changes_and_preserved_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / "app.py").write_text("print('new')\n", encoding="utf-8")
            (public / "app.py").write_text("print('old')\n", encoding="utf-8")
            (public / "README.md").write_text("public docs\n", encoding="utf-8")
            (public / "stale.txt").write_text("stale\n", encoding="utf-8")

            plan = build_plan(source, public, self._manifest())

            self.assertEqual(plan.changed, ("app.py",))
            self.assertEqual(plan.unmanaged, ("stale.txt",))
            self.assertNotIn("README.md", plan.unmanaged)
            self.assertTrue(plan.is_safe)

    def test_line_ending_only_difference_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / "app.py").write_bytes(b"first\r\nsecond\r\n")
            (public / "app.py").write_bytes(b"first\nsecond\n")

            plan = build_plan(source, public, self._manifest())

            self.assertEqual(plan.unchanged, ("app.py",))
            self.assertFalse(plan.has_drift)

    def test_sync_copies_atomically_and_prunes_only_unmanaged_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / "app.py").write_text("print('new')\n", encoding="utf-8")
            (public / "README.md").write_text("keep me\n", encoding="utf-8")
            (public / "stale.txt").write_text("remove me\n", encoding="utf-8")
            plan = build_plan(source, public, self._manifest())

            apply_plan(
                source,
                public,
                plan,
                prune=True,
                require_clean_source=False,
                require_clean_public=False,
            )

            self.assertEqual((public / "app.py").read_text(encoding="utf-8"), "print('new')\n")
            self.assertEqual((public / "README.md").read_text(encoding="utf-8"), "keep me\n")
            self.assertFalse((public / "stale.txt").exists())
            verified = build_plan(source, public, self._manifest())
            self.assertFalse(verified.has_drift)

    def test_sync_refuses_unmanaged_files_without_prune(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / "app.py").write_text("safe\n", encoding="utf-8")
            (public / "stale.txt").write_text("review first\n", encoding="utf-8")
            plan = build_plan(source, public, self._manifest())

            with self.assertRaises(ReleaseSafetyError):
                apply_plan(
                    source,
                    public,
                    plan,
                    prune=False,
                    require_clean_source=False,
                    require_clean_public=False,
                )
            self.assertFalse((public / "app.py").exists())

    def test_denied_path_and_secret_content_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / ".env").write_text("PASSWORD=value\n", encoding="utf-8")
            (source / "app.py").write_text("key = 'TOKEN_ABC12345'\n", encoding="utf-8")
            manifest = self._manifest(include=(".env", "app.py"))

            plan = build_plan(source, public, manifest)

            self.assertIn("denied_source_path:.env", plan.blocked)
            self.assertIn("forbidden_content:test_token:app.py", plan.blocked)
            self.assertFalse(plan.is_safe)

    def test_public_secret_is_blocked_even_when_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / "app.py").write_text("safe\n", encoding="utf-8")
            (public / ".env").write_text("PASSWORD=value\n", encoding="utf-8")

            plan = build_plan(source, public, self._manifest())

            self.assertIn("denied_public_path:.env", plan.blocked)
            self.assertFalse(plan.is_safe)

    def test_ignored_generated_files_do_not_create_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, public = self._roots(directory)
            (source / "app.py").write_text("safe\n", encoding="utf-8")
            (public / "app.py").write_text("safe\n", encoding="utf-8")
            cache = public / "pkg" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "module.pyc").write_bytes(b"generated")

            plan = build_plan(source, public, self._manifest())

            self.assertFalse(plan.has_drift)
            self.assertTrue(plan.is_safe)

    def test_manifest_rejects_parent_path_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "include": ["../private.txt"],
                        "preserve": [],
                        "deny": [],
                        "ignore": [],
                        "forbidden_content_patterns": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_manifest(path)

    def test_public_root_inside_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            public = source / "public"
            public.mkdir(parents=True)
            (source / "app.py").write_text("safe\n", encoding="utf-8")

            with self.assertRaises(ReleaseSafetyError):
                build_plan(source, public, self._manifest())


if __name__ == "__main__":
    unittest.main()
