"""Codex会話用の固定計画、状態記録、画像候補の取り込み。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError

from image_workbench.config import ConfigError, load_config
from image_workbench.ids import parse_uuid4
from image_workbench.models import (
    Candidate,
    ExecutionEvent,
    Generation,
    ReferenceIndex,
    ReferenceSnapshot,
    RequirementSnapshot,
)
from image_workbench.projects import get_project, list_projects, project_root
from image_workbench.prompt_builder import (
    PromptError,
    build_final_prompt,
    find_direct_requirement_conflicts,
)
from image_workbench.references import list_references
from image_workbench.requirements_store import list_requirements
from image_workbench.storage import (
    LockConflict,
    ProjectLock,
    StorageError,
    atomic_copy_file,
    atomic_write_bytes,
    atomic_write_model,
    read_model,
    safe_project_path,
    sha256_file,
)


class GenerationPlanError(ValueError):
    """実行可能な生成・編集計画を作成できない。"""


class GenerationExecutionError(RuntimeError):
    """生成計画の状態変更または出力取込みの安全な失敗。"""

    def __init__(self, classification: str, message: str, *, exit_code: int = 5) -> None:
        self.classification = classification
        self.exit_code = exit_code
        super().__init__(message)


_IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}


def _resolved_count(
    count: int | None, *, config_path: Path | None
) -> tuple[int, Literal["command_line", "config.toml", "built_in"]]:
    overrides = {"default_candidate_count": count} if count is not None else None
    try:
        resolved = load_config(config_path, cli_overrides=overrides)
    except ConfigError as exc:
        raise GenerationPlanError(str(exc)) from exc
    value = count if count is not None else resolved.values.default_candidate_count
    if value > resolved.values.max_candidate_count:
        raise GenerationPlanError(
            f"希望枚数は1〜{resolved.values.max_candidate_count}の範囲で指定してください。"
        )
    source = "command_line" if count is not None else resolved.sources["default_candidate_count"]
    return value, source


def _reference_snapshots(
    project_id: UUID,
    reference_ids: list[str | UUID],
    *,
    projects_dir: Path | None,
) -> list[ReferenceSnapshot]:
    references = {item.id: item for item in list_references(project_id, projects_dir=projects_dir)}
    snapshots: list[ReferenceSnapshot] = []
    seen: set[UUID] = set()
    for raw_id in reference_ids:
        reference_id = parse_uuid4(raw_id)
        if reference_id in seen:
            raise GenerationPlanError(f"参照IDが重複しています: {reference_id}")
        seen.add(reference_id)
        reference = references.get(reference_id)
        if reference is None:
            raise GenerationPlanError(
                f"指定参照画像がこの案件にありません: {reference_id}。"
                "案件IDと参照IDを確認してください。"
            )
        snapshots.append(
            ReferenceSnapshot(
                id=reference.id,
                sha256=reference.sha256,
                role=reference.role,
                description=reference.description,
                priority=reference.priority,
                submitted=True,
            )
        )
    return snapshots


def _requirement_snapshots(
    project_id: UUID, *, projects_dir: Path | None
) -> list[RequirementSnapshot]:
    return [
        RequirementSnapshot(
            id=item.id,
            description=item.description,
            category=item.category,
            priority=item.priority,
        )
        for item in list_requirements(project_id, projects_dir=projects_dir)
    ]


def _candidate_for_project(
    project_id: UUID,
    candidate_id: str | UUID,
    *,
    projects_dir: Path | None,
) -> tuple[Candidate, Path]:
    normalized_id = parse_uuid4(candidate_id)
    root = project_root(project_id, projects_dir=projects_dir)
    candidate_path = root / "candidates" / f"{normalized_id}.json"
    if not candidate_path.is_file():
        raise GenerationPlanError(f"編集元候補がこの案件にありません: {normalized_id}")
    candidate = read_model(candidate_path, Candidate)
    if candidate.project_id != project_id:
        raise GenerationPlanError("編集元候補が別案件に属しています。")
    image_path = safe_project_path(root, candidate.file)
    if not image_path.is_file() or sha256_file(image_path) != candidate.sha256:
        raise GenerationPlanError("編集元候補の画像が見つからないか、hashが一致しません。")
    return candidate, image_path


def _plan_hash_payload(generation: Generation) -> dict[str, Any]:
    return generation.model_dump(
        mode="json",
        exclude={
            "plan_hash",
            "status",
            "approval",
            "candidate_ids",
            "actual_output_count",
            "registered_candidate_count",
            "conversation_context",
            "errors",
            "approved_at",
            "started_at",
            "finished_at",
        },
    )


def calculate_plan_hash(generation: Generation) -> str:
    payload = json.dumps(
        _plan_hash_payload(generation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _with_plan_hash(generation: Generation) -> Generation:
    values = generation.model_dump(mode="json")
    values["plan_hash"] = calculate_plan_hash(generation)
    return Generation.model_validate(values)


def _validate_reference_files(generation: Generation, root: Path) -> None:
    index = read_model(root / "references" / "reference-index.json", ReferenceIndex)
    references = {item.id: item for item in index.references}
    for snapshot in generation.reference_snapshots:
        reference = references.get(snapshot.id)
        if reference is None or reference.sha256 != snapshot.sha256:
            raise GenerationPlanError(f"参照画像のスナップショットが一致しません: {snapshot.id}")
        file_path = safe_project_path(root, reference.file)
        if not file_path.is_file() or sha256_file(file_path) != snapshot.sha256:
            raise GenerationPlanError(f"参照画像が見つからないか、hashが一致しません: {snapshot.id}")


def verify_plan(generation: Generation, root: Path) -> None:
    """保存済み計画と参照・編集元ファイルのスナップショットを再検証する。"""
    if calculate_plan_hash(generation) != generation.plan_hash:
        raise GenerationPlanError("計画内容が作成後に変更されています。新しい計画を作成してください。")
    _validate_reference_files(generation, root)
    if generation.source_candidate_id is not None:
        candidate, image_path = _candidate_for_project(
            generation.project_id,
            generation.source_candidate_id,
            projects_dir=root.parent,
        )
        snapshot = generation.source_candidate_snapshot or {}
        if snapshot.get("sha256") != candidate.sha256 or sha256_file(image_path) != snapshot.get("sha256"):
            raise GenerationPlanError("編集元候補が計画時のスナップショットから変更されています。")


def _create_plan(
    project_id: str | UUID,
    *,
    operation: Literal["generate", "edit"],
    edit_mode: Literal["none", "whole"],
    prompt: str,
    reference_ids: list[str | UUID] | None,
    count: int | None,
    source_candidate_id: str | UUID | None,
    config_path: Path | None,
    projects_dir: Path | None,
) -> Generation:
    try:
        project = get_project(project_id, projects_dir=projects_dir)
    except ValueError as exc:
        raise GenerationPlanError(f"案件IDが不正です: {exc}") from exc
    except LookupError as exc:
        raise GenerationPlanError(str(exc)) from exc
    if project.status != "active":
        raise GenerationPlanError("アーカイブ済み案件では計画を作成できません。")

    requested_count, count_source = _resolved_count(count, config_path=config_path)
    references = _reference_snapshots(project.id, reference_ids or [], projects_dir=projects_dir)
    requirements = _requirement_snapshots(project.id, projects_dir=projects_dir)
    conflicts = find_direct_requirement_conflicts(requirements)
    if conflicts:
        details = "、".join(f"{left.id} / {right.id}" for left, right in conflicts)
        raise GenerationPlanError(f"維持条件と変更条件に直接矛盾があります: {details}")

    source_candidate: Candidate | None = None
    if source_candidate_id is not None:
        source_candidate, _ = _candidate_for_project(
            project.id, source_candidate_id, projects_dir=projects_dir
        )
    if operation == "edit" and source_candidate is None:
        raise GenerationPlanError("編集計画には編集元候補が必要です。")
    if operation == "generate" and source_candidate is not None:
        raise GenerationPlanError("新規生成計画に編集元候補は指定できません。")

    try:
        codex_instruction = build_final_prompt(
            project_goal=project.goal,
            original_instruction=prompt,
            references=references,
            requirements=requirements,
            requested_count_hint=requested_count,
        )
    except PromptError as exc:
        raise GenerationPlanError(str(exc)) from exc

    root = project_root(project.id, projects_dir=projects_dir)
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise GenerationPlanError("アーカイブ済み案件では計画を作成できません。")
        references = _reference_snapshots(project.id, reference_ids or [], projects_dir=projects_dir)
        requirements = _requirement_snapshots(project.id, projects_dir=projects_dir)
        requested_count, count_source = _resolved_count(count, config_path=config_path)
        conflicts = find_direct_requirement_conflicts(requirements)
        if conflicts:
            details = "、".join(f"{left.id} / {right.id}" for left, right in conflicts)
            raise GenerationPlanError(f"維持条件と変更条件に直接矛盾があります: {details}")
        try:
            codex_instruction = build_final_prompt(
                project_goal=current_project.goal,
                original_instruction=prompt,
                references=references,
                requirements=requirements,
                requested_count_hint=requested_count,
            )
        except PromptError as exc:
            raise GenerationPlanError(str(exc)) from exc
        source_candidate = None
        if source_candidate_id is not None:
            source_candidate, _ = _candidate_for_project(
                project.id, source_candidate_id, projects_dir=projects_dir
            )

        now = datetime.now(timezone.utc)
        generations_root = root / "generations"
        generations_root.mkdir(parents=True, exist_ok=True)
        while True:
            generation_id = uuid4()
            final_dir = generations_root / str(generation_id)
            if final_dir.exists():
                continue
            temp_dir = Path(tempfile.mkdtemp(prefix=".iwb-plan-", dir=generations_root))
            try:
                (temp_dir / "events").mkdir()
                (temp_dir / "outputs").mkdir()
                generation = Generation(
                    id=generation_id,
                    project_id=project.id,
                    operation=operation,
                    edit_mode=edit_mode,
                    status="draft",
                    original_instruction=prompt,
                    codex_instruction=codex_instruction,
                    reference_snapshots=references,
                    requirements_snapshot=requirements,
                    source_candidate_id=source_candidate.id if source_candidate else None,
                    source_candidate_snapshot=(
                        {
                            "sha256": source_candidate.sha256,
                            "file": source_candidate.file,
                            "width": source_candidate.width,
                            "height": source_candidate.height,
                            "mime_type": source_candidate.mime_type,
                        }
                        if source_candidate else None
                    ),
                    plan_hash="0" * 64,
                    requested_count_hint=requested_count,
                    requested_count_source=count_source,
                    created_at=now,
                )
                generation = _with_plan_hash(generation)
                atomic_write_model(temp_dir / "generation.json", generation)
                event = ExecutionEvent(
                    generation_id=generation.id,
                    sequence=1,
                    timestamp=now,
                    stage="plan",
                    event_type="plan_created",
                    result="success",
                    details={"plan_hash": generation.plan_hash},
                )
                atomic_write_model(temp_dir / "events" / "000001.json", event)
                try:
                    os.rename(temp_dir, final_dir)
                except FileExistsError:
                    continue
                verify_plan(generation, root)
                return generation
            finally:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)


def plan_generate(
    project_id: str | UUID,
    *,
    prompt: str,
    reference_ids: list[str | UUID] | None = None,
    count: int | None = None,
    config_path: Path | None = None,
    projects_dir: Path | None = None,
) -> Generation:
    return _create_plan(
        project_id,
        operation="generate",
        edit_mode="none",
        prompt=prompt,
        reference_ids=reference_ids,
        count=count,
        source_candidate_id=None,
        config_path=config_path,
        projects_dir=projects_dir,
    )


def plan_edit(
    project_id: str | UUID,
    *,
    source_candidate_id: str | UUID,
    prompt: str,
    reference_ids: list[str | UUID] | None = None,
    count: int | None = None,
    config_path: Path | None = None,
    projects_dir: Path | None = None,
) -> Generation:
    return _create_plan(
        project_id,
        operation="edit",
        edit_mode="whole",
        prompt=prompt,
        reference_ids=reference_ids,
        count=count,
        source_candidate_id=source_candidate_id,
        config_path=config_path,
        projects_dir=projects_dir,
    )


def get_generation(
    generation_id: str | UUID, *, projects_dir: Path | None = None
) -> tuple[Generation, Path]:
    normalized_id = parse_uuid4(generation_id)
    for project in list_projects(projects_dir=projects_dir):
        root = project_root(project.id, projects_dir=projects_dir)
        path = root / "generations" / str(normalized_id) / "generation.json"
        if path.is_file():
            generation = read_model(path, Generation)
            if generation.project_id != project.id:
                raise StorageError(f"実験の案件IDと保存場所が一致しません: {path}")
            return generation, root
    raise LookupError(f"実験が見つかりません: {normalized_id}")


def list_generations(
    project_id: str | UUID, *, projects_dir: Path | None = None
) -> list[Generation]:
    project = get_project(project_id, projects_dir=projects_dir)
    root = project_root(project.id, projects_dir=projects_dir)
    records = [
        read_model(path, Generation)
        for path in (root / "generations").glob("*/generation.json")
    ]
    return sorted(records, key=lambda item: (item.created_at, str(item.id)), reverse=True)


def verify_generation_plan(generation_id: str | UUID) -> Generation:
    generation, root = get_generation(generation_id)
    verify_plan(generation, root)
    return generation


def _save_generation(root: Path, generation: Generation) -> None:
    atomic_write_model(
        root / "generations" / str(generation.id) / "generation.json",
        generation,
        replace=True,
    )


def _next_event_sequence(root: Path, generation_id: UUID) -> int:
    event_dir = root / "generations" / str(generation_id) / "events"
    sequences = [
        int(path.stem)
        for path in event_dir.glob("[0-9]*.json")
        if path.stem.isdigit()
    ]
    return max(sequences, default=0) + 1


def _append_event(
    root: Path,
    generation: Generation,
    *,
    stage: str,
    event_type: str,
    result: str,
    details: dict[str, Any] | None = None,
) -> ExecutionEvent:
    sequence = _next_event_sequence(root, generation.id)
    event = ExecutionEvent(
        generation_id=generation.id,
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
        stage=stage,
        event_type=event_type,
        result=result,
        details=details or {},
    )
    event_path = root / "generations" / str(generation.id) / "events" / f"{sequence:06d}.json"
    atomic_write_model(event_path, event)
    return event


def _generation_with(generation: Generation, **changes: Any) -> Generation:
    return Generation.model_validate({**generation.model_dump(mode="json"), **changes})


def _write_report(root: Path, generation: Generation) -> Exception | None:
    try:
        from image_workbench.reporting import write_report

        write_report(root, generation)
        return None
    except Exception as exc:
        return exc


def _commit_generation_transition(
    root: Path,
    previous: Generation,
    updated: Generation,
    *,
    event_type: str,
    result: str,
    details: dict[str, Any],
) -> Generation:
    """状態と監査イベントをロック下で保存し、イベント失敗時は旧状態へ戻す。"""
    try:
        _save_generation(root, updated)
        _append_event(
            root, updated, stage="conversation", event_type=event_type,
            result=result, details=details,
        )
    except Exception as exc:
        try:
            _save_generation(root, previous)
        except Exception as rollback_error:
            raise GenerationExecutionError(
                "transition_recovery_required",
                "状態記録の一部に失敗し、元の状態へ戻せませんでした。再操作せず、generation.jsonとeventsを確認してください。",
                exit_code=6,
            ) from rollback_error
        raise GenerationExecutionError(
            "transition_save_failed",
            "状態と監査イベントを保存できず、状態を変更前へ戻しました。ファイル保存先を確認してください。",
            exit_code=6,
        ) from exc

    return updated


def cancel_generation(
    generation_id: str | UUID,
    *,
    reason: str,
    projects_dir: Path | None = None,
) -> Generation:
    if not reason.strip():
        raise GenerationExecutionError("invalid_input", "取消理由を指定してください。", exit_code=2)
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        project = get_project(generation.project_id, projects_dir=projects_dir)
        if project.status != "active":
            raise GenerationExecutionError("state_conflict", "アーカイブ済み案件は変更できません。", exit_code=4)
        if generation.status not in {"draft", "awaiting_approval"}:
            raise GenerationExecutionError(
                "state_conflict", f"この状態の計画は取消せません: {generation.status}", exit_code=4
            )
        previous = generation
        generation = _generation_with(
            generation,
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            errors=[*generation.errors, {"code": "user_cancelled", "reason": reason.strip()}],
        )
        return _commit_generation_transition(
            root, previous, generation,
            event_type="generation_cancelled", result="cancelled",
            details={"reason": reason.strip()},
        )


def request_generation_approval(
    generation_id: str | UUID, *, projects_dir: Path | None = None
) -> Generation:
    """会話で計画を提示したことを記録し、承認待ちにする。"""
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        if generation.status != "draft":
            raise GenerationExecutionError(
                "state_conflict", f"承認待ちにできない状態です: {generation.status}", exit_code=4
            )
        verify_plan(generation, root)
        project = get_project(generation.project_id, projects_dir=projects_dir)
        if project.status != "active":
            raise GenerationExecutionError("state_conflict", "アーカイブ済み案件です。", exit_code=4)
        previous = generation
        generation = _generation_with(generation, status="awaiting_approval")
        return _commit_generation_transition(
            root, previous, generation, event_type="approval_requested",
            result="awaiting_approval", details={"plan_hash": generation.plan_hash},
        )


def approve_generation(
    generation_id: str | UUID, *, projects_dir: Path | None = None
) -> Generation:
    """会話で得た明示承認と、直後に生成を開始する状態を記録する。"""
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        if generation.status != "awaiting_approval":
            raise GenerationExecutionError(
                "state_conflict", f"承認できない状態です: {generation.status}", exit_code=4
            )
        verify_plan(generation, root)
        project = get_project(generation.project_id, projects_dir=projects_dir)
        if project.status != "active":
            raise GenerationExecutionError("state_conflict", "アーカイブ済み案件です。", exit_code=4)
        previous = generation
        now = datetime.now(timezone.utc)
        generation = _generation_with(
            generation,
            status="running",
            approval={"recorded_at": now.isoformat(), "plan_hash": generation.plan_hash,
                      "source": "codex_conversation"},
            approved_at=now,
            started_at=now,
        )
        return _commit_generation_transition(
            root, previous, generation, event_type="generation_approved_and_started",
            result="running", details={"plan_hash": generation.plan_hash},
        )


def block_generation(
    generation_id: str | UUID,
    *,
    reason_code: Literal[
        "usage_limit_reached", "codex_imagegen_unavailable", "output_unavailable"
    ],
    message: str,
    projects_dir: Path | None = None,
) -> Generation:
    """生成開始前に停止した理由を記録する。"""
    if not message.strip():
        raise GenerationExecutionError("invalid_input", "停止理由を入力してください。", exit_code=2)
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        project = get_project(generation.project_id, projects_dir=projects_dir)
        if project.status != "active":
            raise GenerationExecutionError("state_conflict", "アーカイブ済み案件は変更できません。", exit_code=4)
        if generation.status not in {"draft", "awaiting_approval"}:
            raise GenerationExecutionError(
                "state_conflict", f"生成前停止として記録できない状態です: {generation.status}", exit_code=4
            )
        now = datetime.now(timezone.utc)
        previous = generation
        error = {"code": reason_code, "message": message.strip(), "generation_started": False}
        generation = _generation_with(
            generation, status="blocked", errors=[*generation.errors, error], finished_at=now
        )
        return _commit_generation_transition(
            root, previous, generation, event_type="generation_blocked",
            result="blocked", details=error,
        )


def mark_generation_unknown(
    generation_id: str | UUID, *, reason: str, projects_dir: Path | None = None
) -> Generation:
    """実行後に結果を確定できない状態を記録し、自動再実行を防ぐ。"""
    if not reason.strip():
        raise GenerationExecutionError("invalid_input", "結果不明の理由を入力してください。", exit_code=2)
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        if generation.status != "running":
            raise GenerationExecutionError(
                "state_conflict", f"結果不明にできない状態です: {generation.status}", exit_code=4
            )
        error = {"code": "result_unknown", "message": reason.strip(), "generation_started": True}
        previous = generation
        generation = _generation_with(
            generation, status="result_unknown", errors=[*generation.errors, error]
        )
        return _commit_generation_transition(
            root, previous, generation, event_type="generation_result_unknown",
            result="result_unknown", details=error,
        )


def fail_generation(
    generation_id: str | UUID,
    *,
    reason_code: Literal[
        "codex_imagegen_unavailable", "output_transfer_failed", "disk_full", "permission_denied"
    ],
    message: str,
    projects_dir: Path | None = None,
) -> Generation:
    """生成開始後の明確な失敗を記録する。"""
    if not message.strip():
        raise GenerationExecutionError("invalid_input", "失敗理由を入力してください。", exit_code=2)
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        if generation.status not in {"running", "result_unknown"} or generation.candidate_ids:
            raise GenerationExecutionError(
                "state_conflict", "出力が未登録で、実行中または結果不明の計画だけ失敗確定できます。", exit_code=4
            )
        now = datetime.now(timezone.utc)
        previous = generation
        error = {"code": reason_code, "message": message.strip(), "generation_started": True}
        generation = _generation_with(
            generation, status="failed", errors=[*generation.errors, error], finished_at=now
        )
        return _commit_generation_transition(
            root, previous, generation, event_type="generation_failure_confirmed",
            result="failed", details=error,
        )


def _inspect_image(path: Path) -> tuple[str, str, int, int, str]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"画像ファイルが見つかりません: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"画像パスが通常ファイルではありません: {resolved}")
    try:
        with Image.open(resolved) as image:
            image_format = image.format
            image.verify()
        with Image.open(resolved) as image:
            image.load()
            width, height = image.size
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError(f"画像をデコードできません: {resolved}") from exc
    if image_format not in _IMAGE_FORMATS:
        raise ValueError(f"対応していない画像形式です: {image_format or 'unknown'}")
    extension, mime_type = _IMAGE_FORMATS[image_format]
    return str(resolved), extension, width, height, mime_type


def register_generation_files(
    generation_id: str | UUID,
    image_paths: list[Path],
    *,
    conversation_context: dict[str, Any] | None = None,
    projects_dir: Path | None = None,
) -> Generation:
    """Codex会話から受け渡された画像を検証し、新しい候補として保存する。"""
    if not image_paths:
        raise GenerationExecutionError("invalid_input", "登録する画像ファイルを指定してください。", exit_code=2)
    generation, root = get_generation(generation_id, projects_dir=projects_dir)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id, projects_dir=projects_dir)
        if generation.status not in {"running", "result_unknown"}:
            raise GenerationExecutionError(
                "state_conflict", f"この状態の計画には画像を登録できません: {generation.status}", exit_code=4
            )
        try:
            verify_plan(generation, root)
            inputs: list[tuple[Path, str, int, int, str, str]] = []
            seen_hashes: set[str] = set()
            existing_hashes: dict[str, UUID] = {}
            for candidate_id in generation.candidate_ids:
                candidate = read_model(root / "candidates" / f"{candidate_id}.json", Candidate)
                existing_hashes[candidate.sha256] = candidate.id
            for path in image_paths:
                resolved_text, extension, width, height, mime_type = _inspect_image(path)
                resolved = Path(resolved_text)
                digest = sha256_file(resolved)
                if digest in seen_hashes:
                    raise GenerationExecutionError(
                        "duplicate_output", f"同じ画像を重複指定しています: {resolved}", exit_code=3
                    )
                if digest in existing_hashes:
                    raise GenerationExecutionError(
                        "duplicate_output",
                        f"この計画に同じ画像が既に登録されています: {existing_hashes[digest]}",
                        exit_code=4,
                    )
                seen_hashes.add(digest)
                inputs.append((resolved, extension, width, height, mime_type, digest))
        except GenerationExecutionError:
            raise
        except (OSError, StorageError, ValueError) as exc:
            raise GenerationExecutionError("invalid_image", str(exc), exit_code=3) from exc

        _append_event(
            root,
            generation,
            stage="handoff",
            event_type="codex_conversation_outputs_received",
            result="success",
            details={"actual_output_count": len(inputs)},
        )
        generation = _generation_with(
            generation,
            actual_output_count=(generation.actual_output_count or 0) + len(inputs),
            conversation_context={**generation.conversation_context, **(conversation_context or {})},
        )
        _save_generation(root, generation)

        saved_ids = list(generation.candidate_ids)
        errors = list(generation.errors)
        output_dir = root / "generations" / str(generation.id) / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        for source, extension, width, height, mime_type, digest in inputs:
            candidate_id = uuid4()
            relative_file = f"generations/{generation.id}/outputs/{candidate_id}.{extension}"
            destination = safe_project_path(root, relative_file)
            try:
                copied_hash = atomic_copy_file(source, destination)
                if copied_hash != digest or sha256_file(destination) != digest:
                    raise StorageError("保存後の画像hashが一致しません。")
                _inspect_image(destination)
                candidate = Candidate(
                    id=candidate_id,
                    generation_id=generation.id,
                    project_id=generation.project_id,
                    file=relative_file,
                    sha256=digest,
                    width=width,
                    height=height,
                    mime_type=mime_type,
                    parent_candidate_id=generation.source_candidate_id,
                    created_at=datetime.now(timezone.utc),
                )
                atomic_write_model(root / "candidates" / f"{candidate_id}.json", candidate)
                saved_ids.append(candidate_id)
                generation = _generation_with(
                    generation,
                    candidate_ids=saved_ids,
                    registered_candidate_count=len(saved_ids),
                )
                _save_generation(root, generation)
                _append_event(
                    root,
                    generation,
                    stage="storage",
                    event_type="candidate_registered",
                    result="success",
                    details={"candidate_id": str(candidate_id), "file": relative_file,
                             "sha256": digest, "width": width, "height": height},
                )
            except Exception as exc:
                errors.append({"code": "candidate_save_failed", "sha256": digest,
                               "error_type": type(exc).__name__})
                generation = _generation_with(generation, errors=errors)
                _save_generation(root, generation)

        final_status = (
            "succeeded" if not errors
            else "partially_succeeded" if saved_ids
            else "failed"
        )
        generation = _generation_with(
            generation,
            status=final_status,
            finished_at=datetime.now(timezone.utc),
            actual_output_count=len(inputs),
            registered_candidate_count=len(saved_ids),
        )
        _save_generation(root, generation)
        _append_event(
            root,
            generation,
            stage="conversation",
            event_type="generation_outputs_registered",
            result=final_status,
            details={
                "requested_count_hint": generation.requested_count_hint,
                "actual_output_count": len(inputs),
                "registered_candidate_count": len(saved_ids),
            },
        )
        report_error = _write_report(root, generation)
        if report_error is not None:
            generation = _generation_with(
                generation,
                errors=[*generation.errors, {"code": "report_write_failed",
                                             "error_type": type(report_error).__name__}],
            )
            _save_generation(root, generation)
            raise GenerationExecutionError(
                "report_write_failed", "候補画像は保存済みですが、レポートを保存できませんでした。", exit_code=6
            ) from report_error
        return generation
