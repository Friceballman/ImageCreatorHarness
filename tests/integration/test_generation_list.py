"""案件ごとの全Generation一覧、全文表示、キャッシュ表示を検証する。"""

from __future__ import annotations

import shutil
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from PIL import Image
from typer.testing import CliRunner

from image_workbench.cli import app
from image_workbench import list_view
from image_workbench.generations import list_generations, plan_generate
from image_workbench.projects import create_project
from image_workbench.references import add_reference
from image_workbench.storage import atomic_write_model


class GenerationListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(__file__).resolve().parents[2] / "tmp" / f"generation-list-test-{uuid4().hex}"
        self.work.mkdir(parents=True)
        self.projects = self.work / "projects"
        self.project = create_project("計画一覧検証", "一覧の確認", projects_dir=self.projects)
        self.root = self.projects / self.project.slug
        image = self.work / "reference.png"
        Image.new("RGB", (10, 10), "red").save(image)
        self.reference = add_reference(
            self.project.id, image, role="design", description="参照の説明",
            projects_dir=self.projects,
        )
        self.first = plan_generate(
            self.project.id, prompt="古い計画\n[bold]日本語の指示[/bold]", count=1,
            reference_ids=[self.reference.id], projects_dir=self.projects,
        )
        self.second = plan_generate(
            self.project.id, prompt="新しい編集計画", count=2, projects_dir=self.projects,
        )
        # 生成処理を呼ばず、一覧表示用の状態・編集元のfixtureを設定する。
        self.first = self.first.model_copy(update={"status": "succeeded"})
        self.second = self.second.model_copy(update={
            "operation": "edit", "edit_mode": "whole", "status": "cancelled",
            "source_candidate_id": uuid4(),
        })
        for generation in (self.first, self.second):
            atomic_write_model(self.path(generation.id), generation, replace=True)
        self.runner = CliRunner()

    def tearDown(self) -> None:
        shutil.rmtree(self.work)

    def path(self, identifier) -> Path:
        return self.root / "generations" / str(identifier) / "generation.json"

    def cli(self, *arguments):
        with chdir(self.work):
            return self.runner.invoke(app, ["generate", "list", *arguments], env={"COLUMNS": "12"})

    def test_all_operations_and_terminal_states_are_listed_newest_first(self) -> None:
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        result = self.cli(str(self.project.id), "--terminal")
        self.assertEqual(result.exit_code, 0, result.output)
        for value in (str(self.first.id), str(self.second.id), str(self.reference.id),
                      self.first.plan_hash, "succeeded", "cancelled", "edit", "[bold]日本語の指示[/bold]"):
            self.assertIn(value, result.output)
        self.assertLess(result.output.index(str(self.second.id)), result.output.index(str(self.first.id)))
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*.json")}, before)

    def test_interactive_list_uses_persistent_cache_and_never_waits(self) -> None:
        process = Mock()
        with patch.object(list_view, "cache_root", return_value=self.work / "cache"), patch.object(
            list_view, "is_interactive", return_value=True,
        ), patch.object(list_view, "_editor_command", side_effect=lambda path: (["code", str(path)], False)), patch.object(
            list_view, "_start_editor", return_value=process,
        ):
            result = self.cli(str(self.project.id))
        self.assertEqual(result.exit_code, 0, result.output)
        process.wait.assert_not_called()
        snapshot, = (self.work / "cache").glob("*/listing.txt")
        self.assertIn(str(self.first.id), snapshot.read_text(encoding="utf-8"))
        self.assertIn(str(self.reference.id), snapshot.read_text(encoding="utf-8"))

    def test_empty_project_and_archived_project_can_be_read(self) -> None:
        empty = create_project("空案件", "一覧確認", projects_dir=self.projects)
        result = self.cli(str(empty.id))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("件数: 0", result.output)
        atomic_write_model(self.root / "project.json", self.project.model_copy(update={"status": "archived"}), replace=True)
        result = self.cli(str(self.project.id), "--terminal")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(self.first.id), result.output)

    def test_other_projects_and_unpublished_staging_records_are_not_included(self) -> None:
        other = create_project("別案件", "対象外", projects_dir=self.projects)
        other_plan = plan_generate(other.id, prompt="別の計画", count=1, projects_dir=self.projects)
        temporary = self.root / "generations" / ".iwb-plan-test"
        temporary.mkdir()
        atomic_write_model(temporary / "generation.json", other_plan)
        records = list_generations(self.project.id, projects_dir=self.projects)
        self.assertEqual({record.id for record in records}, {self.first.id, self.second.id})

    def test_foreign_project_and_mismatched_plan_ids_are_rejected(self) -> None:
        for changes in ({"project_id": uuid4()}, {"id": uuid4()}):
            with self.subTest(changes=changes):
                atomic_write_model(self.path(self.first.id), self.first.model_copy(update=changes), replace=True)
                result = self.cli(str(self.project.id), "--terminal")
                self.assertEqual(result.exit_code, 6, result.output)

    def test_corrupt_or_unknown_schema_is_not_hidden(self) -> None:
        for content in ("{broken", self.first.model_dump_json().replace('"schema_version":1', '"schema_version":999')):
            with self.subTest(content=content[:20]):
                self.path(self.first.id).write_text(content, encoding="utf-8")
                result = self.cli(str(self.project.id), "--terminal")
                self.assertEqual(result.exit_code, 6, result.output)

    def test_invalid_or_missing_project_id(self) -> None:
        for identifier, expected in (("invalid", 2), (str(uuid4()), 4)):
            result = self.cli(identifier)
            self.assertEqual(result.exit_code, expected, result.output)


if __name__ == "__main__":
    unittest.main()
