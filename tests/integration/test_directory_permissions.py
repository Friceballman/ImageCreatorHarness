"""案件・計画の原子的作成とWindowsのACL継承の回帰検証。"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from image_workbench.projects import create_project


def dacl_is_protected(path: Path) -> bool:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = advapi.GetFileSecurityW
    get_security.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    get_security.restype = wintypes.BOOL
    get_control = advapi.GetSecurityDescriptorControl
    get_control.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_control.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    get_security(str(path), 4, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(needed.value)
    if not get_security(str(path), 4, descriptor, needed.value, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(control.value & 0x1000)  # SE_DACL_PROTECTED


class DirectoryPermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        # pytest/tmpfileの0o700を避け、実際のワークスペースの継承ACLで検証する。
        self.root = Path(__file__).resolve().parents[2] / "tmp" / f"permissions-test-{uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def cli(self, *arguments: str) -> None:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "image_workbench.cli", *arguments],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_project_and_plan_remain_readable_after_publication(self) -> None:
        self.cli("project", "create", "--name", "権限検証", "--goal", "保存経路の検証")
        project_root, = (self.root / "projects").glob("project-*")
        project = json.loads((project_root / "project.json").read_text(encoding="utf-8"))
        self.cli("project", "show", project["id"])
        self.cli("generate", "plan", project["id"], "--prompt", "動作確認用", "--count", "1")
        generation_root, = (project_root / "generations").iterdir()
        generation = json.loads((generation_root / "generation.json").read_text(encoding="utf-8"))
        self.assertEqual(generation["status"], "draft")
        self.assertEqual(generation["project_id"], project["id"])
        self.cli("generate", "show", generation["id"])
        self.assertTrue((generation_root / "events" / "000001.json").is_file())
        self.assertEqual(list((generation_root / "outputs").iterdir()), [])
        self.assertEqual(list((self.root / "projects").glob(".iwb-create-*")), [])
        self.assertEqual(list((project_root / "generations").glob(".iwb-plan-*")), [])
        if os.name == "nt":
            for path in (project_root, generation_root):
                self.assertFalse(dacl_is_protected(path), str(path))

    def test_failed_project_creation_does_not_publish_partial_data(self) -> None:
        projects = self.root / "projects"
        with patch("image_workbench.projects.atomic_write_model", side_effect=OSError("検証用の保存失敗")):
            with self.assertRaises(OSError):
                create_project("権限検証", "保存失敗時の保全", projects_dir=projects)
        self.assertEqual(list(projects.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
