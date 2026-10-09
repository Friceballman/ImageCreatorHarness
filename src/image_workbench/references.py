"""参照画像の検証、重複検出、登録、メタデータ履歴。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from image_workbench.ids import parse_uuid4
from image_workbench.models import Reference, ReferenceHistory, ReferenceIndex
from image_workbench.projects import ProjectStateConflict, get_project, project_root
from image_workbench.storage import (
    LockConflict,
    ProjectLock,
    StorageError,
    atomic_copy_file,
    atomic_write_model,
    read_model,
    sha256_file,
)


_EXTENSION_FORMATS = {
    ".png": ("PNG", "image/png", ".png"),
    ".jpg": ("JPEG", "image/jpeg", ".jpg"),
    ".jpeg": ("JPEG", "image/jpeg", ".jpg"),
    ".webp": ("WEBP", "image/webp", ".webp"),
}


class InvalidImageError(ValueError):
    """参照画像のパス・形式・内容が不正。"""


class DuplicateReferenceError(ValueError):
    """既存画像と同じハッシュの登録について判断が必要。"""

    def __init__(self, existing: Reference) -> None:
        self.existing = existing
        super().__init__(
            f"同じ画像が既に登録されています: {existing.id} "
            f"(役割: {existing.role}, ファイル: {existing.file})"
        )


@dataclass(frozen=True)
class ImageFacts:
    source: Path
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    extension: str
    sha256: str


def inspect_reference_image(image_path: Path) -> ImageFacts:
    """拡張子とデコードされた実形式を検証し、SHA-256を計算する。"""
    try:
        source = image_path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise InvalidImageError(f"参照画像が見つかりません: {image_path}") from exc
    if not source.is_file():
        raise InvalidImageError(f"参照画像は通常ファイルを指定してください: {source}")

    expected = _EXTENSION_FORMATS.get(source.suffix.lower())
    if expected is None:
        raise InvalidImageError("参照画像はPNG、JPEG、WebPのいずれかを指定してください。")
    expected_format, mime_type, normalized_extension = expected
    try:
        with Image.open(source) as image:
            actual_format = image.format
            image.verify()
        with Image.open(source) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise InvalidImageError("画像の幅と高さは1以上である必要があります。")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise InvalidImageError(f"画像をデコードできません: {source}") from exc
    if actual_format != expected_format:
        raise InvalidImageError(
            f"拡張子と画像データの形式が一致しません: {source.suffix} / {actual_format}"
        )
    return ImageFacts(
        source=source,
        mime_type=mime_type,
        extension=normalized_extension,
        sha256=sha256_file(source),
    )


def _read_index(root: Path) -> ReferenceIndex:
    return read_model(root / "references" / "reference-index.json", ReferenceIndex)


def _find_hash(index: ReferenceIndex, digest: str) -> Reference | None:
    return next((item for item in index.references if item.sha256 == digest), None)


def list_references(
    project_id: str | UUID, *, projects_dir: Path | None = None
) -> list[Reference]:
    project = get_project(project_id, projects_dir=projects_dir)
    index = _read_index(project_root(project.id, projects_dir=projects_dir))
    return sorted(index.references, key=lambda item: (item.created_at, str(item.id)))


def find_duplicate_reference(
    project_id: str | UUID,
    image_path: Path,
    *,
    projects_dir: Path | None = None,
) -> Reference | None:
    facts = inspect_reference_image(image_path)
    project = get_project(project_id, projects_dir=projects_dir)
    index = _read_index(project_root(project.id, projects_dir=projects_dir))
    return _find_hash(index, facts.sha256)


def add_reference(
    project_id: str | UUID,
    image_path: Path,
    *,
    role: Literal[
        "pose", "silhouette", "design", "face", "color",
        "orthographic", "part", "style", "other",
    ],
    description: str,
    priority: int = 3,
    duplicate_action: Literal["reuse", "share"] | None = None,
    projects_dir: Path | None = None,
) -> Reference:
    """画像を検証して案件へ登録する。重複時は呼出元に判断を求める。"""
    facts = inspect_reference_image(image_path)
    project = get_project(project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
    root = project_root(project.id, projects_dir=projects_dir)

    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
        index = _read_index(root)
        duplicate = _find_hash(index, facts.sha256)
        if duplicate is not None and duplicate_action is None:
            raise DuplicateReferenceError(duplicate)
        if duplicate is not None and duplicate_action == "reuse":
            return duplicate

        reference_id = uuid4()
        if duplicate is not None and duplicate_action == "share":
            relative_file = duplicate.file
        else:
            relative_file = f"references/images/{reference_id}{facts.extension}"
        reference = Reference(
            id=reference_id,
            file=relative_file,
            original_filename=facts.source.name,
            sha256=facts.sha256,
            mime_type=facts.mime_type,
            role=role,
            description=description,
            priority=priority,
            created_at=datetime.now(timezone.utc),
        )

        copied_destination: Path | None = None
        if duplicate is None:
            copied_destination = root / Path(*relative_file.split("/"))
            copied_hash = atomic_copy_file(facts.source, copied_destination)
            try:
                if copied_hash != facts.sha256 or sha256_file(copied_destination) != facts.sha256:
                    raise StorageError("コピー後の画像SHA-256が元画像と一致しません。")
                with Image.open(copied_destination) as copied_image:
                    copied_image.verify()
                with Image.open(copied_destination) as copied_image:
                    if copied_image.format != _EXTENSION_FORMATS[facts.source.suffix.lower()][0]:
                        raise StorageError("コピー後の画像形式が元画像と一致しません。")
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError, StorageError) as exc:
                copied_destination.unlink(missing_ok=True)
                if isinstance(exc, StorageError):
                    raise
                raise StorageError(f"コピー後の画像を再検証できません: {copied_destination}") from exc
        updated_index = ReferenceIndex(references=[*index.references, reference])
        try:
            atomic_write_model(root / "references" / "reference-index.json", updated_index, replace=True)
        except Exception:
            if copied_destination is not None:
                copied_destination.unlink(missing_ok=True)
            raise
        return reference


def show_reference(
    project_id: str | UUID,
    reference_id: str | UUID,
    *,
    projects_dir: Path | None = None,
) -> Reference:
    normalized_id = parse_uuid4(reference_id)
    for reference in list_references(project_id, projects_dir=projects_dir):
        if reference.id == normalized_id:
            return reference
    raise LookupError(f"参照画像が見つかりません: {normalized_id}")


def update_reference(
    project_id: str | UUID,
    reference_id: str | UUID,
    *,
    role: Literal[
        "pose", "silhouette", "design", "face", "color",
        "orthographic", "part", "style", "other",
    ] | None = None,
    description: str | None = None,
    priority: int | None = None,
    reason: str,
    projects_dir: Path | None = None,
) -> Reference:
    if role is None and description is None and priority is None:
        raise ValueError("変更項目を1つ以上指定してください。")
    project = get_project(project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
    root = project_root(project.id, projects_dir=projects_dir)
    with ProjectLock(root):
        if get_project(project.id, projects_dir=projects_dir).status != "active":
            raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
        index_path = root / "references" / "reference-index.json"
        index = _read_index(root)
        normalized_id = parse_uuid4(reference_id)
        current = next((item for item in index.references if item.id == normalized_id), None)
        if current is None:
            raise LookupError(f"参照画像が見つかりません: {normalized_id}")

        changes: dict[str, object] = {}
        if role is not None:
            changes["role"] = role
        if description is not None:
            changes["description"] = description
        if priority is not None:
            changes["priority"] = priority
        updated = Reference.model_validate(
            {**current.model_dump(mode="json"), **changes}
        )
        history = ReferenceHistory(
            id=uuid4(),
            reference_id=current.id,
            timestamp=datetime.now(timezone.utc),
            before={key: getattr(current, key) for key in changes},
            after={key: getattr(updated, key) for key in changes},
            reason=reason,
        )
        updated_index = ReferenceIndex(
            references=[
                updated if item.id == current.id else item for item in index.references
            ]
        )
        history_path = root / "references" / "history" / str(current.id) / f"{history.id}.json"
        atomic_write_model(history_path, history)
        try:
            atomic_write_model(index_path, updated_index, replace=True)
        except Exception:
            history_path.unlink(missing_ok=True)
            raise
        return updated


def reference_history(
    project_id: str | UUID,
    reference_id: str | UUID,
    *,
    projects_dir: Path | None = None,
) -> list[ReferenceHistory]:
    reference = show_reference(project_id, reference_id, projects_dir=projects_dir)
    root = project_root(project_id, projects_dir=projects_dir)
    history_root = root / "references" / "history" / str(reference.id)
    if not history_root.exists():
        return []
    records = [read_model(path, ReferenceHistory) for path in history_root.glob("*.json")]
    return sorted(records, key=lambda event: (event.timestamp, str(event.id)))
