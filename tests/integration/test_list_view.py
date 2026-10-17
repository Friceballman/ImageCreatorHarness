"""一覧キャッシュの永続性、待機しない起動、明示削除の保全検証。"""

from __future__ import annotations

import io
import os
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from typer.testing import CliRunner

from image_workbench.cli import app
from image_workbench import list_view as view
from image_workbench.storage import atomic_write_model, read_model


class ListViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(__file__).resolve().parents[2] / "tmp" / f"list-test-{uuid4().hex}"
        self.work.mkdir(parents=True)
        self.root = self.work / "cache"
        self.root_patch = patch.object(view, "cache_root", return_value=self.root)
        self.cli_root_patch = patch("image_workbench.cli.cache_root", return_value=self.root)
        self.root_patch.start()
        self.cli_root_patch.start()
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.identifier = str(uuid4())
        self.records = [[("ID", self.identifier), ("説明", "[bold]日本語[/bold]\n" + "長文" * 200)]]
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.cli_root_patch.stop()
        self.root_patch.stop()
        shutil.rmtree(self.work)

    def show(self, *, terminal: bool = False) -> None:
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            view.show_listing("検証", self.records, terminal=terminal)

    def editor(self, path: Path):
        return ["fake-code", str(path)], False

    def cache(self) -> Path:
        return view._write_cache("検証", view.render_listing("検証", self.records))

    def test_returns_without_waiting_and_keeps_snapshot(self) -> None:
        process = Mock()
        process.wait.side_effect = AssertionError("エディタの終了を待機してはいけません")
        with patch.object(view, "is_interactive", return_value=True), patch.object(
            view, "_editor_command", side_effect=self.editor,
        ), patch.object(view, "_start_editor", return_value=process) as start:
            self.show()
        process.wait.assert_not_called()
        process.terminate.assert_not_called()
        directory, = self.root.iterdir()
        text = (directory / "listing.txt").read_text(encoding="utf-8")
        self.assertIn(self.identifier, text)
        self.assertIn("[bold]日本語[/bold]", text)
        self.assertIn("長文" * 200, text)
        self.assertIn("取得日時(UTC)", text)
        self.assertNotIn("--wait", start.call_args.args[0])
        self.assertNotIn("閉じて", self.stdout.getvalue())
        record = read_model(directory / "manifest.json", view.ListCache)
        self.assertEqual(str(record.id), directory.name)

    def test_repeated_calls_create_independent_snapshots(self) -> None:
        first = self.cache()
        content = first.read_bytes()
        self.records[0][0] = ("ID", str(uuid4()))
        second = self.cache()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), content)
        self.assertNotEqual(second.read_bytes(), content)
        self.assertEqual(len(view.list_caches()[0]), 2)

    def test_noninteractive_and_terminal_do_not_create_cache(self) -> None:
        for interactive, terminal in ((False, False), (True, True)):
            with self.subTest(interactive=interactive, terminal=terminal), patch.object(
                view, "is_interactive", return_value=interactive,
            ), patch.object(view, "_start_editor") as start:
                self.show(terminal=terminal)
                start.assert_not_called()
                self.assertFalse(self.root.exists())
                self.assertIn(self.identifier, self.stdout.getvalue())

    def test_missing_or_failed_editor_keeps_cache_and_prints_full_text(self) -> None:
        for failure in ("discovery", "launch"):
            with self.subTest(failure=failure), patch.object(view, "is_interactive", return_value=True), patch.object(
                view, "_editor_command", side_effect=OSError("未導入") if failure == "discovery" else self.editor,
            ), patch.object(view, "_start_editor", side_effect=OSError("起動失敗")):
                self.show()
                self.assertIn(self.identifier, self.stdout.getvalue())
        self.assertEqual(len(view.list_caches()[0]), 2)

    def test_cli_clear_single_cache_and_keep_other_and_project_data(self) -> None:
        first, second = self.cache(), self.cache()
        project = self.work / "projects" / "image.png"
        project.parent.mkdir()
        project.write_bytes("元画像".encode("utf-8"))
        result = self.runner.invoke(app, ["cache", "clear", first.parent.name], input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(project.read_bytes(), "元画像".encode("utf-8"))

    def test_clear_all_does_not_touch_cache_created_after_confirmation(self) -> None:
        first = self.cache()
        after = []

        def confirm(*args, **kwargs):
            after.append(self.cache())
            return True

        with patch("image_workbench.cli.typer.confirm", side_effect=confirm):
            result = self.runner.invoke(app, ["cache", "clear"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(first.exists())
        self.assertTrue(after[0].exists())

    def test_clear_all_is_allowed_while_editor_is_open_and_never_terminates_it(self) -> None:
        process = Mock()
        with patch.object(view, "is_interactive", return_value=True), patch.object(
            view, "_editor_command", side_effect=self.editor,
        ), patch.object(view, "_start_editor", return_value=process):
            self.show()
        result = self.runner.invoke(app, ["cache", "clear"], input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(list(self.root.iterdir()), [])
        process.wait.assert_not_called()
        process.terminate.assert_not_called()

    def test_cancellation_empty_and_invalid_ids(self) -> None:
        empty = self.runner.invoke(app, ["cache", "clear"])
        self.assertEqual(empty.exit_code, 0, empty.output)
        self.assertFalse(self.root.exists())
        path = self.cache()
        result = self.runner.invoke(app, ["cache", "clear"], input="n\n")
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertTrue(path.exists())
        for identifier, expected in (("../outside", 2), (str(uuid4()), 4)):
            with self.subTest(identifier=identifier):
                result = self.runner.invoke(app, ["cache", "clear", identifier])
                self.assertEqual(result.exit_code, expected, result.output)

    def test_unknown_schema_corrupt_metadata_and_extra_files_are_preserved(self) -> None:
        unknown, corrupt, extra = self.cache(), self.cache(), self.cache()
        metadata = unknown.parent / "manifest.json"
        metadata.write_text(metadata.read_text(encoding="utf-8").replace('"schema_version": 1', '"schema_version": 999'), encoding="utf-8")
        (corrupt.parent / "manifest.json").write_text("{invalid", encoding="utf-8")
        (extra.parent / "user-note.txt").write_text("削除対象外", encoding="utf-8")
        records, warnings = view.list_caches()
        self.assertEqual(records, [])
        self.assertEqual(len(warnings), 3)
        result = self.runner.invoke(app, ["cache", "clear"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(all(path.exists() for path in (unknown, corrupt, extra)))

    def test_mismatched_cache_id_is_preserved(self) -> None:
        path = self.cache()
        record = read_model(path.parent / "manifest.json", view.ListCache)
        atomic_write_model(
            path.parent / "manifest.json", record.model_copy(update={"id": uuid4()}), replace=True,
        )
        self.assertEqual(view.list_caches()[0], [])
        removed, errors = view.clear_caches([record.id])
        self.assertEqual(removed, 0)
        self.assertTrue(errors)
        self.assertTrue(path.exists())

    def test_unexpected_file_added_after_confirmation_is_preserved(self) -> None:
        path = self.cache()

        def confirm(*args, **kwargs):
            (path.parent / "user-note.txt").write_text("保持する", encoding="utf-8")
            return True

        with patch("image_workbench.cli.typer.confirm", side_effect=confirm):
            result = self.runner.invoke(app, ["cache", "clear"])
        self.assertEqual(result.exit_code, 6, result.output)
        self.assertTrue(path.exists())
        self.assertTrue((path.parent / "user-note.txt").exists())

    def test_edited_snapshot_can_be_explicitly_deleted(self) -> None:
        path = self.cache()
        path.write_text("VS Codeで編集した内容", encoding="utf-8")
        result = self.runner.invoke(app, ["cache", "clear", path.parent.name], input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(path.exists())

    def test_partial_clear_reports_failure_and_removes_other_cache(self) -> None:
        first, second = self.cache(), self.cache()
        original = os.rename

        def fail_one(source, destination):
            if source == first.parent:
                raise PermissionError("検証用の削除失敗")
            return original(source, destination)

        with patch.object(view.os, "rename", side_effect=fail_one):
            result = self.runner.invoke(app, ["cache", "clear"], input="y\n")
        self.assertEqual(result.exit_code, 6, result.output)
        self.assertIn("1件", result.output)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())

    def test_failed_deletion_restores_cache_for_retry(self) -> None:
        path = self.cache()
        original = Path.unlink

        def fail_listing(entry, *args, **kwargs):
            if entry.name == "listing.txt":
                raise PermissionError("検証用の削除失敗")
            return original(entry, *args, **kwargs)

        with patch.object(Path, "unlink", fail_listing):
            result = self.runner.invoke(app, ["cache", "clear"], input="y\n")
        self.assertEqual(result.exit_code, 6, result.output)
        self.assertTrue(path.exists())
        self.assertEqual(len(view.list_caches()[0]), 1)
        result = self.runner.invoke(app, ["cache", "clear"], input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(path.exists())

    def test_reparse_cache_and_incomplete_creation_are_not_deleted(self) -> None:
        path = self.cache()
        creating = self.root / ".creating-test"
        creating.mkdir()
        (creating / "listing.txt").write_text("作成中", encoding="utf-8")
        original = Path.is_junction

        def junction(entry):
            return entry == path.parent or original(entry)

        with patch.object(Path, "is_junction", junction):
            self.assertEqual(view.list_caches()[0], [])
            removed, errors = view.clear_caches([view.parse_uuid4(path.parent.name)])
        self.assertEqual(removed, 0)
        self.assertTrue(errors)
        self.assertTrue(path.exists())
        self.assertTrue((creating / "listing.txt").exists())

    def test_write_failure_does_not_publish_partial_cache(self) -> None:
        with patch.object(view, "atomic_write_model", side_effect=OSError("検証用の保存失敗")):
            with self.assertRaises(OSError):
                self.cache()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_start_never_deletes_existing_cache(self) -> None:
        from image_workbench import cli

        path = self.cache()
        before = path.read_bytes()
        with patch.object(cli, "app") as app_mock:
            cli.main()
        app_mock.assert_called_once_with()
        self.assertEqual(path.read_bytes(), before)

    def test_windows_launcher_uses_executable_without_wait_and_without_shell(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows専用のランチャー検証")
        install = self.work / "VS Code 日本語 & test"
        launcher = install / "bin" / "code.cmd"
        cli = install / "version123" / "resources" / "app" / "out" / "cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("", encoding="utf-8")
        executable = install / "Code.exe"
        executable.write_bytes(b"")
        launcher.parent.mkdir()
        launcher.write_text('"%~dp0..\\Code.exe" "%~dp0..\\version123\\resources\\app\\out\\cli.js" %*', encoding="utf-8")
        with patch.object(view.shutil, "which", return_value=str(launcher)):
            command, electron = view._editor_command(self.work / "一覧 &.txt")
        self.assertEqual(command[:2], [str(executable), str(cli)])
        self.assertNotIn("--wait", command)
        self.assertTrue(electron)
        with patch.object(view.subprocess, "Popen") as popen, patch.dict(
            os.environ, {"ELECTRON_RUN_AS_NODE": "old", "VSCODE_DEV": "old"},
        ):
            view._start_editor(command, electron)
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(popen.call_args.args[0], command)
            self.assertEqual(os.environ["ELECTRON_RUN_AS_NODE"], "old")
            self.assertEqual(os.environ["VSCODE_DEV"], "old")

    def test_all_four_commands_keep_complete_ids_in_narrow_terminal(self) -> None:
        record = SimpleNamespace(
            id=self.identifier, name="[bold]名前[/bold]", status="selected", goal="目的" * 100,
            category="must_keep", priority=2, description="説明" * 200, role="design",
            file="references/images/a.png", generation_id=uuid4(), parent_candidate_id=uuid4(),
            width=20, height=30, sha256="a" * 64,
        )
        for kind, function in (("project", "list_projects"), ("reference", "list_references"),
                               ("requirement", "list_requirements"), ("candidate", "list_candidates")):
            with self.subTest(kind=kind), patch(f"image_workbench.cli.{function}", return_value=[record]) as listing, patch.object(
                view, "is_interactive", return_value=False,
            ), patch.object(view, "_start_editor") as start:
                arguments = [kind, "list"] + ([] if kind == "project" else [str(uuid4())])
                if kind == "candidate":
                    arguments += ["--generation", str(record.generation_id)]
                result = self.runner.invoke(app, arguments, env={"COLUMNS": "12"})
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn(self.identifier, result.output)
                if kind == "candidate":
                    self.assertIn(str(record.generation_id), result.output)
                    self.assertIn(str(record.parent_candidate_id), result.output)
                    self.assertIn(record.sha256, result.output)
                    self.assertEqual(listing.call_args.kwargs["generation_id"], str(record.generation_id))
                start.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_zero_items_and_terminal_option_do_not_open_editor(self) -> None:
        with patch("image_workbench.cli.list_projects", return_value=[]), patch.object(
            view, "is_interactive", return_value=True,
        ), patch.object(view, "_start_editor") as start:
            for arguments in (["project", "list"], ["project", "list", "--terminal"]):
                result = self.runner.invoke(app, arguments)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("件数: 0", result.output)
            start.assert_not_called()
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
