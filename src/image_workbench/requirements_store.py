"""案件要件と追記専用の変更履歴。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from image_workbench.ids import parse_uuid4
from image_workbench.models import Requirement, RequirementHistory, RequirementStore
from image_workbench.projects import ProjectStateConflict, get_project, project_root
from image_workbench.storage import ProjectLock, StorageError, atomic_write_model, read_model


class RequirementNotFound(LookupError):
    """指定された要件が案件内に存在しない。"""


def _read_store(root: Path) -> RequirementStore:
    return read_model(root / "requirements.json", RequirementStore)


def list_requirements(
    project_id: str | UUID,
    *,
    include_retired: bool = False,
    projects_dir: Path | None = None,
) -> list[Requirement]:
    project = get_project(project_id, projects_dir=projects_dir)
    store = _read_store(project_root(project.id, projects_dir=projects_dir))
    items = store.requirements
    if not include_retired:
        items = [item for item in items if item.status == "active"]
    return sorted(items, key=lambda item: (item.priority, str(item.id)))


def add_requirement(
    project_id: str | UUID,
    *,
    category: Literal["must_keep", "must_change", "improve"],
    description: str,
    priority: int = 3,
    projects_dir: Path | None = None,
) -> Requirement:
    project = get_project(project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
    root = project_root(project.id, projects_dir=projects_dir)
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
        store = _read_store(root)
        item = Requirement(
            id=uuid4(),
            description=description,
            category=category,
            priority=priority,
            status="active",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        updated = RequirementStore(requirements=[*store.requirements, item])
        atomic_write_model(root / "requirements.json", updated, replace=True)
        return item


def get_requirement(
    project_id: str | UUID,
    requirement_id: str | UUID,
    *,
    include_retired: bool = True,
    projects_dir: Path | None = None,
) -> Requirement:
    normalized_id = parse_uuid4(requirement_id)
    for item in list_requirements(
        project_id, include_retired=include_retired, projects_dir=projects_dir
    ):
        if item.id == normalized_id:
            return item
    raise RequirementNotFound(f"要件が見つかりません: {normalized_id}")


def _save_change(
    root: Path,
    store: RequirementStore,
    old_requirement: Requirement,
    new_requirement: Requirement,
    *,
    action: Literal["updated", "retired"],
    reason: str,
) -> Requirement:
    history = RequirementHistory(
        id=uuid4(),
        requirement_id=old_requirement.id,
        timestamp=datetime.now(timezone.utc),
        action=action,
        before=old_requirement.model_dump(mode="json", exclude={"schema_version"}),
        after=new_requirement.model_dump(mode="json", exclude={"schema_version"}),
        reason=reason,
    )
    updated_store = RequirementStore(
        requirements=[
            new_requirement if item.id == old_requirement.id else item
            for item in store.requirements
        ]
    )
    history_path = root / "requirement-history" / str(old_requirement.id) / f"{history.id}.json"
    atomic_write_model(history_path, history)
    try:
        atomic_write_model(root / "requirements.json", updated_store, replace=True)
    except Exception:
        history_path.unlink(missing_ok=True)
        raise
    return new_requirement


def update_requirement(
    project_id: str | UUID,
    requirement_id: str | UUID,
    *,
    category: Literal["must_keep", "must_change", "improve"] | None = None,
    description: str | None = None,
    priority: int | None = None,
    reason: str,
    projects_dir: Path | None = None,
) -> Requirement:
    if category is None and description is None and priority is None:
        raise ValueError("更新項目を1つ以上指定してください。")
    project = get_project(project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
    root = project_root(project.id, projects_dir=projects_dir)
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
        store = _read_store(root)
        current = _find_requirement(store, requirement_id)
        if current.status != "active":
            raise ValueError("廃止済み要件は更新できません。")
        changes: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if category is not None:
            changes["category"] = category
        if description is not None:
            changes["description"] = description
        if priority is not None:
            changes["priority"] = priority
        updated = Requirement.model_validate(
            {**current.model_dump(mode="json"), **changes}
        )
        return _save_change(
            root, store, current, updated, action="updated", reason=reason
        )


def retire_requirement(
    project_id: str | UUID,
    requirement_id: str | UUID,
    *,
    reason: str,
    projects_dir: Path | None = None,
) -> Requirement:
    project = get_project(project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
    root = project_root(project.id, projects_dir=projects_dir)
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise ProjectStateConflict("アーカイブ済み案件は変更できません。")
        store = _read_store(root)
        current = _find_requirement(store, requirement_id)
        if current.status != "active":
            raise ValueError("要件は既に廃止されています。")
        updated = Requirement.model_validate(
            {
                **current.model_dump(mode="json"),
                "status": "retired",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return _save_change(
            root, store, current, updated, action="retired", reason=reason
        )


def _find_requirement(store: RequirementStore, requirement_id: str | UUID) -> Requirement:
    normalized_id = parse_uuid4(requirement_id)
    for item in store.requirements:
        if item.id == normalized_id:
            return item
    raise RequirementNotFound(f"要件が見つかりません: {normalized_id}")


def requirement_history(
    project_id: str | UUID,
    requirement_id: str | UUID | None = None,
    *,
    projects_dir: Path | None = None,
) -> list[RequirementHistory]:
    project = get_project(project_id, projects_dir=projects_dir)
    root = project_root(project.id, projects_dir=projects_dir)
    selected_id = parse_uuid4(requirement_id) if requirement_id is not None else None
    history_root = root / "requirement-history"
    if not history_root.exists():
        return []
    records: list[RequirementHistory] = []
    for path in history_root.glob("*/*.json"):
        event = read_model(path, RequirementHistory)
        if selected_id is None or event.requirement_id == selected_id:
            records.append(event)
    return sorted(records, key=lambda event: (event.timestamp, str(event.id)))
