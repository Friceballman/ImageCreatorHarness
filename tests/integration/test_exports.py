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
    ExportNameError, ExportRecord, _publish_export, export_filenames,
    export_selected_candidates, selected_candidates,
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

    def test_named_cli_exports_in_id_order_and_preserves_all_source_data(self) -> None:
        atomic_write_model(
            self.root / "project.json",
            self.project.model_copy(update={"approved_candidate_id": self.candidates[0].id}), replace=True,
        )
        for status in ("pending", "rejected", "on_hold"):
            candidate = self.candidates[0].model_copy(update={"id": uuid4(), "status": status})
            atomic_write_model(self.root / "candidates" / f"{candidate.id}.json", candidate)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        # 有効な装飾記法風の名前を、狭い端末でもそのまま表示する。
        name = "[bold]ロボット 立ち絵" + "長い名前" * 15
        with patch.dict("os.environ", {"COLUMNS": "12"}):
            result = self.cli("project", "export", str(self.project.id), "--name", name)
        self.assertEqual(result.exit_code, 0, result.output)
        directory, = (self.root / "exports").iterdir()
        record = read_model(directory / "manifest.json", ExportRecord)
        self.assertEqual([item.candidate_id for item in record.images],
                         sorted((c.id for c in self.candidates), key=str))
        self.assertIn(f"出力先: {directory}", result.output)
        for index, item in enumerate(record.images, 1):
            filename = f"{name}_{index:03d}.png"
            self.assertIn(f"{item.candidate_id} → {filename}", result.output)
            self.assertEqual(Path(item.file).name, filename)
            self.assertEqual((self.root / item.file).read_bytes(), before[self.root / item.source_file])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        first = {p: p.read_bytes() for p in directory.iterdir()}
        again = self.cli("project", "export", str(self.project.id), "--name", name)
        self.assertEqual(again.exit_code, 0, again.output)
        self.assertEqual(len(list((self.root / "exports").iterdir())), 2)
        for path, content in first.items():
            self.assertEqual(path.read_bytes(), content)

    def test_named_single_candidate_has_no_sequence(self) -> None:
        other = self.candidates[1].model_copy(update={"status": "on_hold"})
        atomic_write_model(self.root / "candidates" / f"{other.id}.json", other, replace=True)
        result = self.cli("project", "export", str(self.project.id), "--name", "ロボット")
        self.assertEqual(result.exit_code, 0, result.output)
        directory, = (self.root / "exports").iterdir()
        record = read_model(directory / "manifest.json", ExportRecord)
        self.assertEqual(Path(record.images[0].file).name, "ロボット.png")
        self.assertIn(f"{self.candidates[0].id} → ロボット.png", result.output)

    def test_named_mixed_formats_share_one_sequence_without_reencoding(self) -> None:
        candidates = [self.candidates[0]]
        for image_format, extension, mime in (("JPEG", ".jpg", "image/jpeg"),
                                               ("WEBP", ".webp", "image/webp")):
            original = self.candidates[1]
            relative = f"generations/{original.generation_id}/outputs/{uuid4()}{extension}"
            Image.new("RGB", (12, 15), "green").save(self.root / relative, format=image_format)
            candidates.append(original.model_copy(update={
                "id": uuid4(), "file": relative, "mime_type": mime,
                "sha256": sha256_file(self.root / relative),
            }))
        with ProjectLock(self.root):
            record = _publish_export(self.root, self.project, candidates, name="成果")
        self.assertEqual([Path(item.file).name for item in record.images],
                         ["成果_001.png", "成果_002.jpg", "成果_003.webp"])
        for item in record.images:
            self.assertEqual((self.root / item.file).read_bytes(), (self.root / item.source_file).read_bytes())

    def test_invalid_names_are_argument_errors_without_any_export(self) -> None:
        names = ["", " ", ".", "..", "末尾 ", "末尾.", "末尾\t", "a\x00b", "a\x1fb", "a\x7fb",
                 "../out", "C:\\out", "a/b", "a<b", "a>b", 'a"b', "a:b", "a|b", "a?b", "a*b",
                 "CON", "con.png", "PRN", "AUX", "NUL", "COM1", "lpt9", "COM¹", "LPT².txt",
                 "COM³", "a" * 256, "😀" * 128]
        for name in names:
            with self.subTest(name=repr(name)):
                result = self.cli("project", "export", str(self.project.id), "--name", name)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("出力名が不正", result.output)
                self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_filename_length_includes_sequence_extension_and_utf16_units(self) -> None:
        one = self.candidates[:1]
        self.assertEqual(export_filenames(one, "a" * 251), ["a" * 251 + ".png"])
        self.assertEqual(export_filenames(one, "😀" * 125 + "a"), ["😀" * 125 + "a.png"])
        for candidates, name in ((one, "a" * 252), (self.candidates, "a" * 251),
                                 (one, "😀" * 126)):
            with self.subTest(name=name):
                with self.assertRaises(ExportNameError):
                    export_filenames(candidates, name)
        result = self.cli("project", "export", str(self.project.id), "--name", "a" * 251)
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_sequence_expands_past_three_digits(self) -> None:
        names = export_filenames([self.candidates[0]] * 1001, "成果")
        self.assertEqual(names[0], "成果_001.png")
        self.assertEqual(names[999:], ["成果_1000.png", "成果_1001.png"])
        self.assertEqual(len(set(names)), 1001)

    def test_named_export_cancellation_creates_no_files(self) -> None:
        result = self.cli("project", "export", str(self.project.id), "--name", "成果", answer="n\n")
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertIn("成果_001.png", result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_zero_selected_validates_name_without_creating_output(self) -> None:
        for candidate in self.candidates:
            atomic_write_model(self.root / "candidates" / f"{candidate.id}.json",
                               candidate.model_copy(update={"status": "pending"}), replace=True)
        valid = self.cli("project", "export", str(self.project.id), "--name", "成果")
        self.assertEqual(valid.exit_code, 0, valid.output)
        invalid = self.cli("project", "export", str(self.project.id), "--name", "NUL")
        self.assertEqual(invalid.exit_code, 2, invalid.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_name_omission_still_exports_candidate_uuid_names(self) -> None:
        result = self.cli("project", "export", str(self.project.id))
        self.assertEqual(result.exit_code, 0, result.output)
        directory, = (self.root / "exports").iterdir()
        self.assertEqual({p.name for p in directory.iterdir()},
                         {f"{c.id}.png" for c in self.candidates} | {"manifest.json"})

    def test_named_confirmation_change_from_single_to_multiple_is_rejected(self) -> None:
        candidate = self.candidates[1]
        path = self.root / "candidates" / f"{candidate.id}.json"
        atomic_write_model(path, candidate.model_copy(update={"status": "pending"}), replace=True)

        def change_selection(*args, **kwargs):
            atomic_write_model(path, candidate, replace=True)
            return True

        with patch("image_workbench.cli.typer.confirm", side_effect=change_selection):
            result = self.cli("project", "export", str(self.project.id), "--name", "成果")
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertIn("→ 成果.png", result.output)
        self.assertIn("確認後", result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_named_copy_failure_does_not_publish_any_output(self) -> None:
        from image_workbench.storage import atomic_copy_file

        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("試験用の保存失敗")
            return atomic_copy_file(source, destination)

        with patch("image_workbench.exports.atomic_copy_file", side_effect=fail_second):
            result = self.cli("project", "export", str(self.project.id), "--name", "成果")
        self.assertEqual(result.exit_code, 6, result.output)
        self.assertEqual(list((self.root / "exports").iterdir()), [])

    def test_manual_flow_register_select_name_and_export(self) -> None:
        from image_workbench.generations import (
            approve_generation, plan_generate, register_generation_files, request_generation_approval,
        )

        project = create_project("操作フロー", "選択して命名出力", projects_dir=self.work)
        generation = plan_generate(project.id, prompt="試験用", count=1, projects_dir=self.work)
        request_generation_approval(generation.id, projects_dir=self.work)
        approve_generation(generation.id, projects_dir=self.work)
        # 生成機能を使わず、既存の試験画像を受け渡し結果として登録する。
        registered = register_generation_files(
            generation.id, [self.root / c.file for c in self.candidates], projects_dir=self.work,
        )
        for identifier in registered.candidate_ids:
            selected = self.cli("candidate", "select", str(identifier), "--reason", "成果物に含めるため")
            self.assertEqual(selected.exit_code, 0, selected.output)
        result = self.cli("project", "export", str(project.id), "--name", "完成画像")
        self.assertEqual(result.exit_code, 0, result.output)
        directory, = (self.work / project.slug / "exports").iterdir()
        record = read_model(directory / "manifest.json", ExportRecord)
        self.assertEqual({i.candidate_id for i in record.images}, set(registered.candidate_ids))
        self.assertEqual([Path(i.file).name for i in record.images], ["完成画像_001.png", "完成画像_002.png"])

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
