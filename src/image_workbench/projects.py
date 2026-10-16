"""案件の初期化、検索、一覧、アーカイブ。"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from image_workbench.config import load_config
from image_workbench.ids import parse_uuid4
from image_workbench.models import (
    Generation,
    Project,
    ReferenceIndex,
    RequirementStore,
)
from image_workbench.storage import (
    ProjectLock,
    StorageError,
    atomic_write_model,
    create_staging_directory,
    read_model,
)


_PROJECT_SLUG = re.compile(r"^project-[0-9a-f]{8}$")


class ProjectStateConflict(RuntimeError):
    """案件の状態により要求された書き込みを実行できない。"""


def _projects_root(projects_dir: Path | None = None) -> Path:
    root = projects_dir or load_config().values.projects_dir
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def _project_directories(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in root.iterdir():
        if not _PROJECT_SLUG.fullmatch(path.name) or not path.is_dir():
            continue
        resolved = path.resolve(strict=True)
        if resolved.parent == root:
            result.append(resolved)
    return result


def _read_project(path: Path) -> Project:
    project = read_model(path / "project.json", Project)
    if path.name != project.slug:
        raise StorageError(f"案件slugと保存フォルダ名が一致しません: {path}")
    return project


def create_project(name: str, goal: str, *, projects_dir: Path | None = None) -> Project:
    """初期JSONと必要ディレクトリを用意し、完成した案件フォルダを公開する。"""
    root = _projects_root(projects_dir)
    now = datetime.now(timezone.utc)

    while True:
        project_id = uuid4()
        slug = f"project-{str(project_id).split('-')[0]}"
        destination = root / slug
        if destination.exists():
            continue

        temporary_root = create_staging_directory(root, prefix=".iwb-create-")
        try:
            (temporary_root / "references").mkdir()
            (temporary_root / "generations").mkdir()
            (temporary_root / "candidates").mkdir()
            (temporary_root / "reviews").mkdir()
            (temporary_root / "reports").mkdir()
            (temporary_root / "exports").mkdir()

            project = Project(
                id=project_id,
                slug=slug,
                name=name,
                goal=goal,
                status="active",
                created_at=now,
                updated_at=now,
            )
            atomic_write_model(temporary_root / "project.json", project)
            atomic_write_model(temporary_root / "requirements.json", RequirementStore())
            atomic_write_model(
                temporary_root / "references" / "reference-index.json",
                ReferenceIndex(),
            )
            try:
                os.rename(temporary_root, destination)
            except FileExistsError:
                continue
            return project
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)


def list_projects(*, projects_dir: Path | None = None) -> list[Project]:
    root = _projects_root(projects_dir)
    projects = [_read_project(path) for path in _project_directories(root)]
    return sorted(projects, key=lambda item: (item.created_at, str(item.id)), reverse=True)


def get_project(project_id: str | UUID, *, projects_dir: Path | None = None) -> Project:
    normalized_id = parse_uuid4(project_id)
    for project in list_projects(projects_dir=projects_dir):
        if project.id == normalized_id:
            return project
    raise LookupError(f"案件が見つかりません: {normalized_id}")


def project_root(project_id: str | UUID, *, projects_dir: Path | None = None) -> Path:
    project = get_project(project_id, projects_dir=projects_dir)
    root = _projects_root(projects_dir)
    return root / project.slug


def archive_project(project_id: str | UUID, *, projects_dir: Path | None = None) -> Project:
    """案件をロックし、activeからarchivedへ更新する。"""
    current = get_project(project_id, projects_dir=projects_dir)
    root = project_root(current.id, projects_dir=projects_dir)
    with ProjectLock(root):
        # ロック取得後に再読込し、競合更新を上書きしない。
        current = _read_project(root)
        if current.status != "active":
            raise ProjectStateConflict("アーカイブ済み案件です。")
        for generation_path in (root / "generations").glob("*/generation.json"):
            generation = read_model(generation_path, Generation)
            if generation.status in {"running", "result_unknown"}:
                raise ProjectStateConflict(
                    f"結果未確定の実験があるためアーカイブできません: {generation.id}"
                )
        archived = Project.model_validate(
            {
                **current.model_dump(mode="json"),
                "status": "archived",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        atomic_write_model(root / "project.json", archived, replace=True)
        return archived
