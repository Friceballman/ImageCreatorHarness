"""エクスポート時の検証、原子的確定、既存データ保全を確認する。"""

from __future__ import annotations

import shutil
import unittest
from contextlib import chdir
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from typer.testing import CliRunner

from image_workbench.cli import app
from image_workbench.exports import (
    ExportRecord, _publish_export, export_selected_candidates, selected_candidates,
)
from image_workbench.models import Candidate
from image_workbench.projects import create_project
from image_workbench.storage import ProjectLock, atomic_write_model, read_model, sha256_file


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(__file__).resolve().parents[2] / "tmp" / f"exports-test-{uuid4().hex}"
        self.work.mkdir(parents=True)
        (self.work / "config.toml").write_text('projects_dir = "."\n', encoding="utf-8")
        self.project = create_project("出力検証", "画像出力", projects_dir=self.work)
        self.root = self.work / self.project.slug
        self.candidates = []
        for color in ("red", "blue"):
            candidate_id, generation_id = uuid4(), uuid4()
            relative = f"generations/{generation_id}/outputs/{candidate_id}.png"
            source = self.root / relative
            source.parent.mkdir(parents=True)
            Image.new("RGB", (12, 15), color).save(source)
            self.candidates.append(Candidate(
                id=candidate_id, generation_id=generation_id,
                project_id=self.project.id, file=relative,
                sha256=sha256_file(source), width=12, height=15,
                mime_type="image/png", created_at=datetime.now(timezone.utc),
                status="selected",
            ))
        for candidate in self.candidates:
            atomic_write_model(self.root / "candidates" / f"{candidate.id}.json", candidate)
        self.runner = CliRunner()

    def cli(self, *arguments: str, answer: str = "y\n"):
        with chdir(self.work):
            return self.runner.invoke(app, list(arguments), input=answer)

    def tearDown(self) -> None:
        shutil.rmtree(self.work)

    def publish(self) -> ExportRecord:
        with ProjectLock(self.root):
            return _publish_export(self.root, self.project, self.candidates)

    def test_verified_batch_has_manifest_and_repeat_preserves_first_export(self) -> None:
        originals = {c.file: (self.root / c.file).read_bytes() for c in self.candidates}
        first = self.publish()
        manifest = self.root / "exports" / str(first.id) / "manifest.json"
        self.assertEqual(read_model(manifest, ExportRecord), first)
        first_bytes = manifest.read_bytes()
        for item in first.images:
            self.assertEqual(sha256_file(self.root / item.file), item.sha256)
            self.assertEqual((self.root / item.file).read_bytes(), originals[item.source_file])
        second = self.publish()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(manifest.read_bytes(), first_bytes)
        self.assertEqual(len(list((self.root / "exports").iterdir())), 2)
        for path, content in originals.items():
            self.assertEqual((self.root / path).read_bytes(), content)

    def test_hash_mismatch_rejects_entire_batch(self) -> None:
        Image.new("RGB", (12, 15), "green").save(self.root / self.candidates[1].file)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.publish()
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_corrupt_image_rejects_entire_batch(self) -> None:
        (self.root / self.candidates[0].file).write_bytes(b"invalid image")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_copy_failure_does_not_publish_partial_export(self) -> None:
        from image_workbench.storage import atomic_copy_file

        existing = self.publish()
        existing_directory = self.root / "exports" / str(existing.id)
        before = {p.name: p.read_bytes() for p in existing_directory.iterdir()}
        calls = 0

        def fail_second(source: Path, destination: Path) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("検証用のディスク障害")
            return atomic_copy_file(source, destination)

        with patch("image_workbench.exports.atomic_copy_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(list((self.root / "exports").iterdir()), [existing_directory])
        self.assertEqual({p.name: p.read_bytes() for p in existing_directory.iterdir()}, before)

    def test_cli_missing_image_is_validation_failure_without_output(self) -> None:
        (self.root / self.candidates[0].file).unlink()
        result = self.cli("project", "export", str(self.project.id))
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_jpeg_and_webp_are_copied_without_reencoding(self) -> None:
        for mime, image_format, extension in (("image/jpeg", "JPEG", ".jpg"), ("image/webp", "WEBP", ".webp")):
            with self.subTest(mime=mime):
                original = self.candidates[0]
                relative = f"generations/{original.generation_id}/outputs/{original.id}{extension}"
                source = self.root / relative
                Image.new("RGB", (12, 15), "red").save(source, format=image_format)
                candidate = original.model_copy(update={
                    "file": relative, "mime_type": mime, "sha256": sha256_file(source),
                })
                with ProjectLock(self.root):
                    record = _publish_export(self.root, self.project, [candidate])
                exported = self.root / record.images[0].file
                self.assertEqual(exported.suffix, extension)
                self.assertEqual(exported.read_bytes(), source.read_bytes())

    def test_cli_exports_all_selected_and_excludes_other_states(self) -> None:
        for status in ("pending", "rejected", "on_hold"):
            candidate = self.candidates[0].model_copy(update={"id": uuid4(), "status": status})
            atomic_write_model(self.root / "candidates" / f"{candidate.id}.json", candidate)
        before = {p: p.read_bytes() for p in (self.root / "candidates").glob("*.json")}
        project_before = (self.root / "project.json").read_bytes()
        result = self.cli("project", "export", str(self.project.id))
        self.assertEqual(result.exit_code, 0, result.output)
        folder, = (self.root / "exports").iterdir()
        record = read_model(folder / "manifest.json", ExportRecord)
        self.assertEqual({i.candidate_id for i in record.images}, {c.id for c in self.candidates})
        self.assertEqual(len(list(folder.iterdir())), 3)
        self.assertEqual((self.root / "project.json").read_bytes(), project_before)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_cli_cancellation_leaves_no_export(self) -> None:
        result = self.cli("project", "export", str(self.project.id), answer="n\n")
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_no_selected_candidates_is_success_without_output(self) -> None:
        for candidate in self.candidates:
            atomic_write_model(
                self.root / "candidates" / f"{candidate.id}.json",
                candidate.model_copy(update={"status": "pending"}), replace=True,
            )
        result = self.cli("project", "export", str(self.project.id), answer="")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("0件", result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_confirmation_snapshot_change_is_rejected(self) -> None:
        expected = selected_candidates(self.project.id, projects_dir=self.work)
        candidate = self.candidates[0]
        atomic_write_model(
            self.root / "candidates" / f"{candidate.id}.json",
            candidate.model_copy(update={"status": "rejected"}), replace=True,
        )
        with self.assertRaisesRegex(RuntimeError, "確認後"):
            export_selected_candidates(self.project.id, expected_candidates=expected, projects_dir=self.work)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_archived_project_is_rejected(self) -> None:
        atomic_write_model(
            self.root / "project.json", self.project.model_copy(update={"status": "archived"}),
            replace=True,
        )
        result = self.cli("project", "export", str(self.project.id))
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_stale_lock_is_preserved(self) -> None:
        lock = self.root / ".iwb.lock"
        lock.write_text("検証用の残存ロック", encoding="utf-8")
        result = self.cli("project", "export", str(self.project.id))
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertEqual(lock.read_text(encoding="utf-8"), "検証用の残存ロック")
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_foreign_candidate_is_rejected(self) -> None:
        candidate = self.candidates[0]
        atomic_write_model(
            self.root / "candidates" / f"{candidate.id}.json",
            candidate.model_copy(update={"project_id": uuid4()}), replace=True,
        )
        result = self.cli("project", "export", str(self.project.id))
        self.assertEqual(result.exit_code, 6, result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_unknown_schema_and_outside_path_are_rejected(self) -> None:
        import json

        candidate = self.candidates[0]
        path = self.root / "candidates" / f"{candidate.id}.json"
        for change in ({"schema_version": 999}, {"file": "../outside.png"}):
            with self.subTest(change=change):
                data = candidate.model_dump(mode="json") | change
                path.write_text(json.dumps(data), encoding="utf-8")
                result = self.cli("project", "export", str(self.project.id))
                self.assertEqual(result.exit_code, 6, result.output)
                self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_invalid_and_missing_project_ids(self) -> None:
        for project_id, expected in (("invalid", 2), (str(uuid4()), 4)):
            with self.subTest(project_id=project_id):
                result = self.cli("project", "export", project_id)
                self.assertEqual(result.exit_code, expected, result.output)


if __name__ == "__main__":
    unittest.main()
