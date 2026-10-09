"""候補参照、レビュー、理由付き状態変更、採用版設定。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from image_workbench.ids import parse_uuid4
from image_workbench.models import (
    Candidate,
    CandidateStatusChange,
    ExecutionEvent,
    Generation,
    Project,
    RequirementResult,
    Review,
)
from image_workbench.projects import get_project, list_projects, project_root
from image_workbench.requirements_store import list_requirements
from image_workbench.storage import ProjectLock, StorageError, atomic_write_model, read_model


def _append_candidate_event(
    root: Path,
    generation_id: UUID,
    *,
    stage: str,
    event_type: str,
    details: dict[str, object],
) -> None:
    generation_dir = root / "generations" / str(generation_id)
    generation = read_model(generation_dir / "generation.json", Generation)
    event_dir = generation_dir / "events"
    sequences = [
        int(path.stem)
        for path in event_dir.glob("[0-9]*.json")
        if path.stem.isdigit()
    ]
    sequence = max(sequences, default=0) + 1
    event = ExecutionEvent(
        generation_id=generation_id,
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
        stage=stage,
        event_type=event_type,
        result="success",
        details=details,
    )
    atomic_write_model(event_dir / f"{sequence:06d}.json", event)


class CandidateNotFound(LookupError):
    """候補が存在しない。"""


class CandidateStateConflict(RuntimeError):
    """候補の状態または案件状態が操作条件を満たさない。"""


class ReviewRequired(CandidateStateConflict):
    """未レビュー候補の採用に人間の追加確認が必要。"""


def find_candidate(
    candidate_id: str | UUID,
    *,
    projects_dir: Path | None = None,
) -> tuple[Candidate, Project, Path]:
    normalized_id = parse_uuid4(candidate_id)
    for project in list_projects(projects_dir=projects_dir):
        root = project_root(project.id, projects_dir=projects_dir)
        path = root / "candidates" / f"{normalized_id}.json"
        if path.is_file():
            candidate = read_model(path, Candidate)
            if candidate.project_id != project.id:
                raise StorageError(f"候補と案件のIDが一致しません: {path}")
            return candidate, project, root
    raise CandidateNotFound(f"候補が見つかりません: {normalized_id}")


def list_candidates(
    project_id: str | UUID,
    *,
    generation_id: str | UUID | None = None,
    projects_dir: Path | None = None,
) -> list[Candidate]:
    project = get_project(project_id, projects_dir=projects_dir)
    root = project_root(project.id, projects_dir=projects_dir)
    normalized_generation = parse_uuid4(generation_id) if generation_id is not None else None
    items = [
        read_model(path, Candidate)
        for path in (root / "candidates").glob("*.json")
    ]
    if normalized_generation is not None:
        items = [item for item in items if item.generation_id == normalized_generation]
    return sorted(items, key=lambda item: (item.created_at, str(item.id)), reverse=True)


def review_candidate(
    candidate_id: str | UUID,
    *,
    requirement_results: list[RequirementResult],
    issues: list[str],
    rationale: str,
    projects_dir: Path | None = None,
) -> Review:
    candidate, project, root = find_candidate(candidate_id, projects_dir=projects_dir)
    if project.status != "active":
        raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
        current = read_model(root / "candidates" / f"{candidate.id}.json", Candidate)
        active_requirements = list_requirements(project.id, projects_dir=projects_dir)
        required_ids = {item.id for item in active_requirements}
        result_ids = [item.requirement_id for item in requirement_results]
        if set(result_ids) != required_ids or len(result_ids) != len(required_ids):
            raise CandidateStateConflict(
                "レビュー中に有効要件が変化しました。要件を再読込してレビューし直してください。"
            )
        review = Review(
            id=uuid4(),
            candidate_id=current.id,
            requirement_results=requirement_results,
            issues=issues,
            rationale=rationale,
            reviewer="human",
            created_at=datetime.now(timezone.utc),
        )
        updated_candidate = Candidate.model_validate(
            {
                **current.model_dump(mode="json"),
                "review_ids": [*current.review_ids, review.id],
            }
        )
        review_path = root / "reviews" / f"{review.id}.json"
        atomic_write_model(review_path, review)
        try:
            atomic_write_model(
                root / "candidates" / f"{current.id}.json",
                updated_candidate,
                replace=True,
            )
            _append_candidate_event(
                root,
                current.generation_id,
                stage="review",
                event_type="candidate_reviewed",
                details={"candidate_id": str(current.id), "review_id": str(review.id)},
            )
        except Exception:
            atomic_write_model(
                root / "candidates" / f"{current.id}.json",
                current,
                replace=True,
            )
            review_path.unlink(missing_ok=True)
            raise
        return review


def update_candidate_status(
    candidate_id: str | UUID,
    *,
    status: Literal["selected", "rejected", "on_hold"],
    reason: str,
    projects_dir: Path | None = None,
) -> Candidate:
    if not reason.strip():
        raise ValueError("状態変更の理由を指定してください。")
    candidate, project, root = find_candidate(candidate_id, projects_dir=projects_dir)
    if project.status != "active":
        raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
    with ProjectLock(root):
        if get_project(project.id, projects_dir=projects_dir).status != "active":
            raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
        current = read_model(root / "candidates" / f"{candidate.id}.json", Candidate)
        if current.status == status:
            raise CandidateStateConflict(f"候補は既に{status}です。")
        change = CandidateStatusChange(
            status=status,
            timestamp=datetime.now(timezone.utc),
            reason=reason,
        )
        updated = Candidate.model_validate(
            {
                **current.model_dump(mode="json"),
                "status": status,
                "status_history": [*current.status_history, change],
            }
        )
        candidate_path = root / "candidates" / f"{candidate.id}.json"
        atomic_write_model(candidate_path, updated, replace=True)
        try:
            _append_candidate_event(
                root,
                current.generation_id,
                stage="candidate",
                event_type="candidate_status_changed",
                details={
                    "candidate_id": str(candidate.id),
                    "before": current.status,
                    "after": status,
                    "reason": reason,
                },
            )
        except Exception:
            atomic_write_model(candidate_path, current, replace=True)
            raise
        return updated


def set_approved_candidate(
    project_id: str | UUID,
    candidate_id: str | UUID,
    *,
    allow_unreviewed: bool = False,
    reason: str | None = None,
    projects_dir: Path | None = None,
) -> Project:
    project = get_project(project_id, projects_dir=projects_dir)
    normalized_candidate_id = parse_uuid4(candidate_id)
    root = project_root(project.id, projects_dir=projects_dir)
    if project.status != "active":
        raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
        candidate_path = root / "candidates" / f"{normalized_candidate_id}.json"
        if not candidate_path.is_file():
            raise CandidateNotFound(f"指定候補はこの案件にありません: {normalized_candidate_id}")
        candidate = read_model(candidate_path, Candidate)
        if candidate.project_id != project.id:
            raise CandidateNotFound("指定候補はこの案件にありません。")
        if candidate.status != "selected":
            raise CandidateStateConflict("採用版に設定できるのはselected候補だけです。")
        if not candidate.review_ids:
            if not allow_unreviewed:
                raise ReviewRequired("未レビュー候補です。明示確認と理由が必要です。")
            if reason is None or not reason.strip():
                raise ValueError("未レビュー候補を採用する理由を指定してください。")

        if current_project.approved_candidate_id == candidate.id:
            return current_project

        updated = Project.model_validate(
            {
                **current_project.model_dump(mode="json"),
                "approved_candidate_id": candidate.id,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        project_path = root / "project.json"
        atomic_write_model(project_path, updated, replace=True)
        try:
            _append_candidate_event(
                root,
                candidate.generation_id,
                stage="approval",
                event_type="project_approved_candidate_changed",
                details={
                    "old_candidate_id": str(current_project.approved_candidate_id)
                    if current_project.approved_candidate_id
                    else None,
                    "new_candidate_id": str(candidate.id),
                    "reason": reason,
                },
            )
        except Exception:
            atomic_write_model(project_path, current_project, replace=True)
            raise
        return updated
