"""検証済み画像の追記型エクスポート。"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import Field, UUID4

from image_workbench.models import (
    Candidate, DataModel, Project, ProjectRelativePath, Record, UTCDateTime,
)
from image_workbench.evaluation import list_candidates
from image_workbench.projects import ProjectStateConflict, get_project, project_root
from image_workbench.storage import (
    ProjectLock, StorageError, atomic_copy_file, atomic_write_model,
    create_staging_directory, safe_project_path, sha256_file,
)


class ExportImage(DataModel):
    candidate_id: UUID4
    generation_id: UUID4
    source_file: ProjectRelativePath
    file: ProjectRelativePath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]


class ExportRecord(Record):
    id: UUID4
    project_id: UUID4
    created_at: UTCDateTime
    selection: Literal["selected"] = "selected"
    images: list[ExportImage]


_FORMATS = {
    "image/png": ("PNG", ".png"),
    "image/jpeg": ("JPEG", ".jpg"),
    "image/webp": ("WEBP", ".webp"),
}


def selected_candidates(
    project_id: str | UUID, *, projects_dir: Path | None = None,
) -> list[Candidate]:
    project = get_project(project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise ProjectStateConflict("アーカイブ済み案件は出力できません。")
    candidates = list_candidates(project.id, projects_dir=projects_dir)
    root = project_root(project.id, projects_dir=projects_dir)
    seen = set()
    for candidate in candidates:
        if (candidate.project_id != project.id or candidate.id in seen or
            not (root / "candidates" / f"{candidate.id}.json").is_file()):
            raise StorageError("候補のIDまたは案件所属が不正です。元データを保持して確認してください。")
        seen.add(candidate.id)
    return sorted(
        (candidate for candidate in candidates if candidate.status == "selected"),
        key=lambda candidate: str(candidate.id),
    )


def export_selected_candidates(
    project_id: str | UUID, *, expected_candidates: list[Candidate] | None = None,
    projects_dir: Path | None = None,
) -> ExportRecord | None:
    """確認済みのselected候補すべてを案件ロック下で再検証して出力する。"""
    root = project_root(project_id, projects_dir=projects_dir)
    with ProjectLock(root):
        project = get_project(project_id, projects_dir=projects_dir)
        candidates = selected_candidates(project.id, projects_dir=projects_dir)
        if expected_candidates is not None and candidates != expected_candidates:
            raise ProjectStateConflict("確認後に候補が変更されました。対象を再確認して出力し直してください。")
        if not candidates:
            return None
        return _publish_export(root, project, candidates)


def _verify_image(path: Path, candidate: Candidate) -> None:
    if not path.is_file():
        raise ValueError(f"候補画像が見つかりません: {candidate.id}")
    try:
        with Image.open(path) as image:
            image_format = image.format
            image.verify()
        with Image.open(path) as image:
            image.load()
            size = image.size
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError(f"候補画像をデコードできません: {candidate.id}") from exc
    if image_format != _FORMATS[candidate.mime_type][0] or size != (candidate.width, candidate.height):
        raise ValueError(f"候補画像の形式または寸法が記録と一致しません: {candidate.id}")
    if sha256_file(path) != candidate.sha256:
        raise ValueError(f"候補画像のSHA-256が記録と一致しません: {candidate.id}")


def _publish_export(root: Path, project: Project, candidates: list[Candidate]) -> ExportRecord:
    """呼出元の案件ロック下で、全画像と一覧を検証してからまとめて公開する。"""
    sources = []
    for candidate in candidates:
        source = safe_project_path(root, candidate.file)
        _verify_image(source, candidate)
        sources.append(source)
    exports_root = safe_project_path(root, "exports")
    exports_root.mkdir(exist_ok=True)
    export_id = uuid4()
    relative_root = f"exports/{export_id}"
    destination = safe_project_path(root, relative_root)
    temporary = create_staging_directory(exports_root, prefix=".iwb-export-")
    try:
        images = []
        for candidate, source in zip(candidates, sources, strict=True):
            filename = f"{candidate.id}{_FORMATS[candidate.mime_type][1]}"
            copied = temporary / filename
            digest = atomic_copy_file(source, copied)
            if digest != candidate.sha256:
                raise StorageError(f"コピー中に候補画像が変更されました: {candidate.id}")
            _verify_image(copied, candidate)
            images.append(ExportImage(
                candidate_id=candidate.id, generation_id=candidate.generation_id,
                source_file=candidate.file, file=f"{relative_root}/{filename}",
                sha256=candidate.sha256, width=candidate.width,
                height=candidate.height, mime_type=candidate.mime_type,
            ))
        record = ExportRecord(
            id=export_id, project_id=project.id,
            created_at=datetime.now(timezone.utc), images=images,
        )
        atomic_write_model(temporary / "manifest.json", record)
        if destination.exists():
            raise StorageError("出力先が既に存在します。既存の出力は上書きしません。")
        os.rename(temporary, destination)
        return record
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
