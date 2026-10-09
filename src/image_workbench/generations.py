"""固定スナップショットの生成・編集計画。"""

from __future__ import annotations

import hashlib
import base64
import binascii
import io
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from image_workbench.config import ConfigError, load_config
from image_workbench.ids import parse_uuid4
from image_workbench.models import (
    Candidate,
    EffectiveSettings,
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
    atomic_write_model,
    read_model,
    safe_project_path,
    sha256_file,
    atomic_write_bytes,
)


class GenerationPlanError(ValueError):
    """実行可能な生成・編集計画を作成できない。"""


class GenerationExecutionError(RuntimeError):
    """実行結果を保存したうえで利用者へ通知する安全な失敗。"""

    def __init__(self, classification: str, message: str, *, exit_code: int = 5) -> None:
        self.classification = classification
        self.exit_code = exit_code
        super().__init__(message)


def _resolved_settings(
    *,
    count: int | None,
    model: str | None,
    size: str | None,
    quality: str | None,
    output_format: str | None,
    config_path: Path | None,
) -> EffectiveSettings:
    overrides: dict[str, Any] = {}
    if count is not None:
        overrides["default_candidate_count"] = count
    if model is not None:
        overrides["default_model"] = model
    if size is not None:
        overrides["default_size"] = size
    if quality is not None:
        overrides["default_quality"] = quality
    if output_format is not None:
        overrides["default_output_format"] = output_format
    try:
        resolved = load_config(config_path, cli_overrides=overrides)
    except ConfigError as exc:
        raise GenerationPlanError(str(exc)) from exc

    values = resolved.values
    return EffectiveSettings(
        model=values.default_model,
        size=values.default_size,
        quality=values.default_quality,
        output_format=values.default_output_format,
        count=values.default_candidate_count,
        sources={
            "model": resolved.sources["default_model"],
            "size": resolved.sources["default_size"],
            "quality": resolved.sources["default_quality"],
            "output_format": resolved.sources["default_output_format"],
            "count": resolved.sources["default_candidate_count"],
        },
    )


def _reference_snapshots(
    project_id: UUID,
    reference_ids: list[str | UUID],
    *,
    projects_dir: Path | None,
) -> list[ReferenceSnapshot]:
    references = {item.id: item for item in list_references(project_id, projects_dir=projects_dir)}
    snapshots: list[ReferenceSnapshot] = []
    for raw_id in reference_ids:
        reference_id = parse_uuid4(raw_id)
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
        raise GenerationPlanError(
            f"編集元候補がこの案件にありません: {normalized_id}"
        )
    candidate = read_model(candidate_path, Candidate)
    if candidate.project_id != project_id:
        raise GenerationPlanError("編集元候補が別案件に属しています。")
    image_path = safe_project_path(root, candidate.file)
    if not image_path.is_file() or sha256_file(image_path) != candidate.sha256:
        raise GenerationPlanError("編集元候補の画像が見つからないか、hashが一致しません。")
    return candidate, image_path


def _validate_mask(mask_path: Path, source_image_path: Path) -> dict[str, Any]:
    try:
        source = mask_path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise GenerationPlanError(f"マスク画像が見つかりません: {mask_path}") from exc
    if not source.is_file() or source.suffix.lower() != ".png":
        raise GenerationPlanError("マスクはPNGの通常ファイルを指定してください。")
    if source.stat().st_size >= 4 * 1024 * 1024:
        raise GenerationPlanError("マスクPNGは4MB未満にしてください。")
    try:
        with Image.open(source) as mask:
            if mask.format != "PNG":
                raise GenerationPlanError("マスク画像の実形式がPNGではありません。")
            if "A" not in mask.getbands():
                raise GenerationPlanError("マスクPNGにはアルファチャンネルが必要です。")
            mask.verify()
        with Image.open(source) as mask:
            mask.load()
            mask_size = mask.size
        with Image.open(source_image_path) as original:
            if original.size != mask_size:
                raise GenerationPlanError(
                    f"マスクと編集元の寸法が一致しません: {mask_size} / {original.size}"
                )
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise GenerationPlanError(f"マスク画像をデコードできません: {source}") from exc
    return {
        "original_filename": source.name,
        "sha256": sha256_file(source),
        "mime_type": "image/png",
        "width": mask_size[0],
        "height": mask_size[1],
        "source_path": str(source),
    }


def _plan_hash_payload(generation: Generation) -> dict[str, Any]:
    return generation.model_dump(
        mode="json",
        exclude={
            "plan_hash",
            "status",
            "approval",
            "candidate_ids",
            "failed_slots",
            "attempts",
            "errors",
            "provider_request_ids",
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
    """計画JSONと、送信元画像・マスクの保存済みhashを再検証する。"""
    if calculate_plan_hash(generation) != generation.plan_hash:
        raise GenerationPlanError("計画内容または設定が作成後に変更されています。新しい計画を作成してください。")
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
    if generation.mask_snapshot:
        mask_path = safe_project_path(root, generation.mask_snapshot["file"])
        if not mask_path.is_file() or sha256_file(mask_path) != generation.mask_snapshot["sha256"]:
            raise GenerationPlanError("保存済みマスクが計画時のスナップショットから変更されています。")


def _create_plan(
    project_id: str | UUID,
    *,
    operation: Literal["generate", "edit"],
    edit_mode: Literal["none", "whole", "masked"],
    prompt: str,
    reference_ids: list[str | UUID] | None,
    count: int | None,
    model: str | None,
    size: str | None,
    quality: str | None,
    output_format: str | None,
    source_candidate_id: str | UUID | None,
    mask_path: Path | None,
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

    settings = _resolved_settings(
        count=count,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        config_path=config_path,
    )
    references = _reference_snapshots(
        project.id, reference_ids or [], projects_dir=projects_dir
    )
    if len(references) + (1 if source_candidate_id is not None else 0) > 16:
        raise GenerationPlanError("APIへ送信する画像は編集元を含めて16枚までです。")
    requirements = _requirement_snapshots(project.id, projects_dir=projects_dir)
    conflicts = find_direct_requirement_conflicts(requirements)
    if conflicts:
        details = "、".join(f"{left.id} / {right.id}" for left, right in conflicts)
        raise GenerationPlanError(f"維持条件と変更条件に直接矛盾があります。計画を作れません: {details}")

    try:
        final_prompt = build_final_prompt(
            project_goal=project.goal,
            original_instruction=prompt,
            references=references,
            requirements=requirements,
            size=settings.size,
            quality=settings.quality,
            output_format=settings.output_format,
        )
    except PromptError as exc:
        raise GenerationPlanError(str(exc)) from exc

    source_candidate: Candidate | None = None
    source_image_path: Path | None = None
    if source_candidate_id is not None:
        source_candidate, source_image_path = _candidate_for_project(
            project.id, source_candidate_id, projects_dir=projects_dir
        )
    if edit_mode == "masked":
        if mask_path is None or source_image_path is None:
            raise GenerationPlanError("masked編集にはマスクと編集元候補が必要です。")
        mask_details = _validate_mask(mask_path, source_image_path)
    else:
        if mask_path is not None:
            raise GenerationPlanError("マスクを指定できるのはmasked編集のみです。")
        mask_details = None

    root = project_root(project.id, projects_dir=projects_dir)
    with ProjectLock(root):
        current_project = get_project(project.id, projects_dir=projects_dir)
        if current_project.status != "active":
            raise GenerationPlanError("アーカイブ済み案件では計画を作成できません。")

        # ロック取得後に再読込し、設定・参照・要件を同一スナップショットに固定する。
        settings = _resolved_settings(
            count=count,
            model=model,
            size=size,
            quality=quality,
            output_format=output_format,
            config_path=config_path,
        )
        references = _reference_snapshots(
            project.id, reference_ids or [], projects_dir=projects_dir
        )
        if len(references) + (1 if source_candidate_id is not None else 0) > 16:
            raise GenerationPlanError("APIへ送信する画像は編集元を含めて16枚までです。")
        requirements = _requirement_snapshots(project.id, projects_dir=projects_dir)
        conflicts = find_direct_requirement_conflicts(requirements)
        if conflicts:
            details = "、".join(f"{left.id} / {right.id}" for left, right in conflicts)
            raise GenerationPlanError(
                f"維持条件と変更条件に直接矛盾があります。計画を作れません: {details}"
            )
        try:
            final_prompt = build_final_prompt(
                project_goal=current_project.goal,
                original_instruction=prompt,
                references=references,
                requirements=requirements,
                size=settings.size,
                quality=settings.quality,
                output_format=settings.output_format,
            )
        except PromptError as exc:
            raise GenerationPlanError(str(exc)) from exc
        source_candidate = None
        source_image_path = None
        if source_candidate_id is not None:
            source_candidate, source_image_path = _candidate_for_project(
                project.id, source_candidate_id, projects_dir=projects_dir
            )
        if edit_mode == "masked":
            if mask_path is None or source_image_path is None:
                raise GenerationPlanError("masked編集にはマスクと編集元候補が必要です。")
            mask_details = _validate_mask(mask_path, source_image_path)
        elif mask_path is not None:
            raise GenerationPlanError("マスクを指定できるのはmasked編集のみです。")
        else:
            mask_details = None
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
                stored_mask: dict[str, Any] | None = None
                if mask_details is not None and mask_path is not None:
                    mask_destination = temp_dir / "mask.png"
                    copied_hash = atomic_copy_file(mask_path.expanduser().resolve(strict=True), mask_destination)
                    if copied_hash != mask_details["sha256"]:
                        raise StorageError("計画保存時にマスク画像のhashが変化しました。")
                    stored_mask = {
                        key: value for key, value in mask_details.items()
                        if key != "source_path"
                    }
                    stored_mask["file"] = f"generations/{generation_id}/mask.png"

                generation = Generation(
                    id=generation_id,
                    project_id=project.id,
                    operation=operation,
                    edit_mode=edit_mode,
                    status="draft",
                    original_instruction=prompt,
                    final_prompt=final_prompt,
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
                        if source_candidate
                        else None
                    ),
                    mask_snapshot=stored_mask,
                    effective_settings=settings,
                    plan_hash="0" * 64,
                    cost_estimate={
                        "status": "unknown",
                        "display": "概算不明",
                        "reason": "要求画像数だけでは入力画像・生成画像の実使用量を確定できません。",
                    },
                    requested_count=settings.count,
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
                try:
                    verify_plan(generation, root)
                except Exception:
                    shutil.rmtree(final_dir)
                    raise
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
    model: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    output_format: str | None = None,
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
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        source_candidate_id=None,
        mask_path=None,
        config_path=config_path,
        projects_dir=projects_dir,
    )


def plan_edit(
    project_id: str | UUID,
    *,
    source_candidate_id: str | UUID,
    mode: Literal["whole", "masked"],
    prompt: str,
    mask_path: Path | None = None,
    reference_ids: list[str | UUID] | None = None,
    count: int | None = None,
    model: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    output_format: str | None = None,
    config_path: Path | None = None,
    projects_dir: Path | None = None,
) -> Generation:
    if mode == "whole" and mask_path is not None:
        raise GenerationPlanError("whole編集にはマスクを指定できません。")
    return _create_plan(
        project_id,
        operation="edit",
        edit_mode=mode,
        prompt=prompt,
        reference_ids=reference_ids,
        count=count,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        source_candidate_id=source_candidate_id,
        mask_path=mask_path,
        config_path=config_path,
        projects_dir=projects_dir,
    )


def plan_retry_failed_slots(
    generation_id: str | UUID, *, projects_dir: Path | None = None
) -> Generation:
    """明確に失敗した枠だけを新しい固定計画として再実行する。"""
    source, root = get_generation(generation_id, projects_dir=projects_dir)
    if source.operation != "generate":
        raise GenerationPlanError("再試行できるのは新規生成の実験だけです。")
    if not source.failed_slots:
        unknown = any(item.get("outcome") == "unknown" for item in source.errors)
        detail = "結果不明の枠は重複課金の可能性があるため自動再送できません。" if unknown else "明確に失敗した候補枠がありません。"
        raise GenerationPlanError(detail)
    if source.status not in {"failed", "partially_succeeded"}:
        raise GenerationPlanError("完了した失敗枠がある実験だけ再試行できます。")
    project = get_project(source.project_id, projects_dir=projects_dir)
    if project.status != "active":
        raise GenerationPlanError("アーカイブ済み案件では再試行計画を作成できません。")
    verify_plan(source, root)

    with ProjectLock(root):
        source, current_root = get_generation(source.id, projects_dir=projects_dir)
        if current_root != root:
            raise GenerationPlanError("元実験の案件が変わりました。")
        slots = sorted(set(source.failed_slots))
        if any(slot < 1 or slot > source.requested_count for slot in slots):
            raise GenerationPlanError("元実験の失敗枠データが不正です。")
        if source.mask_snapshot:
            raise GenerationPlanError("新規生成の再試行にマスク情報が含まれているため実行できません。")

        retry_id = uuid4()
        now = datetime.now(timezone.utc)
        generations_root = root / "generations"
        temp_dir = Path(tempfile.mkdtemp(prefix=".iwb-retry-", dir=generations_root))
        final_dir = generations_root / str(retry_id)
        try:
            (temp_dir / "events").mkdir()
            (temp_dir / "outputs").mkdir()
            settings = source.effective_settings.model_copy(update={"count": len(slots)})
            retry = Generation(
                id=retry_id,
                project_id=source.project_id,
                operation="generate",
                edit_mode="none",
                status="draft",
                original_instruction=source.original_instruction,
                final_prompt=source.final_prompt,
                reference_snapshots=source.reference_snapshots,
                requirements_snapshot=source.requirements_snapshot,
                effective_settings=settings,
                plan_hash="0" * 64,
                cost_estimate=source.cost_estimate,
                requested_count=len(slots),
                created_at=now,
                retry_of_generation_id=source.id,
                errors=[],
            )
            retry = _with_plan_hash(retry)
            atomic_write_model(temp_dir / "generation.json", retry)
            _append_event(
                temp_dir,
                retry,
                stage="plan",
                event_type="retry_plan_created",
                result="success",
                details={"retry_of_generation_id": str(source.id), "source_slots": slots},
            )
            os.rename(temp_dir, final_dir)
            return retry
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)


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


def cancel_generation_before_api(
    generation_id: str | UUID,
    *,
    displayed_plan_hash: str,
) -> Generation:
    """プレビュー後に利用者が拒否した計画をAPI未実行で取消す。"""
    generation, root = get_generation(generation_id)
    with ProjectLock(root):
        generation, _ = get_generation(generation_id)
        if generation.plan_hash != displayed_plan_hash:
            raise GenerationExecutionError(
                "plan_hash_mismatch",
                "プレビュー後に計画が変わりました。状態は変更していません。",
                exit_code=4,
            )
        if generation.status not in {"draft", "awaiting_approval"}:
            raise GenerationExecutionError(
                "state_conflict",
                f"この状態の実験は取消せません: {generation.status}",
                exit_code=4,
            )
        if generation.status == "draft":
            generation = _generation_with(generation, status="awaiting_approval")
            _save_generation(root, generation)
            _append_event(
                root,
                generation,
                stage="approval",
                event_type="approval_previewed",
                result="not_approved",
                details={"plan_hash": displayed_plan_hash},
            )
        generation = _generation_with(
            generation,
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            errors=[*generation.errors, {"code": "user_cancelled", "api_called": False}],
        )
        _save_generation(root, generation)
        _append_event(
            root,
            generation,
            stage="approval",
            event_type="run_cancelled",
            result="cancelled",
            details={"api_called": False},
        )
        report_error = _write_failure_report(root, generation)
        if report_error is not None:
            generation = _generation_with(
                generation,
                errors=[
                    *generation.errors,
                    {"code": "report_write_failed", "error_type": type(report_error).__name__},
                ],
            )
            _save_generation(root, generation)
            raise GenerationExecutionError(
                "report_write_failed",
                "実験はAPI呼び出し前に取り消されましたが、レポートを保存できませんでした。",
                exit_code=6,
            ) from report_error
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


def _safe_attempt_data(attempts: list[Any]) -> list[dict[str, Any]]:
    return [asdict(item) for item in attempts]


def _request_image_paths(generation: Generation, root: Path) -> tuple[list[Path], Path | None]:
    paths: list[Path] = []
    if generation.source_candidate_id is not None:
        source = generation.source_candidate_snapshot or {}
        source_file = source.get("file")
        if not source_file:
            raise GenerationPlanError("編集元候補の保存パスが計画にありません。")
        paths.append(safe_project_path(root, source_file))

    index = read_model(root / "references" / "reference-index.json", ReferenceIndex)
    indexed = {item.id: item for item in index.references}
    for snapshot in generation.reference_snapshots:
        reference = indexed.get(snapshot.id)
        if reference is None or reference.sha256 != snapshot.sha256:
            raise GenerationPlanError(f"参照画像が計画と一致しません: {snapshot.id}")
        paths.append(safe_project_path(root, reference.file))

    mask_path = (
        safe_project_path(root, generation.mask_snapshot["file"])
        if generation.mask_snapshot
        else None
    )
    return paths, mask_path


def _record_candidate(
    generation: Generation,
    root: Path,
    *,
    slot: int,
    image_data: Any,
) -> Candidate:
    encoded = getattr(image_data, "b64_json", None)
    if not encoded:
        raise ValueError("画像応答にbase64データがありません。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("画像応答のbase64データが不正です。") from exc
    if not content:
        raise ValueError("画像応答が空です。")

    expected = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}[
        generation.effective_settings.output_format
    ]
    try:
        with Image.open(io.BytesIO(content)) as image:
            actual_format = image.format
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            width, height = image.size
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("応答画像をデコードできません。") from exc
    if actual_format != expected:
        raise ValueError(
            f"応答画像形式が要求形式と一致しません: {actual_format} / {expected}"
        )

    candidate_id = uuid4()
    extension = generation.effective_settings.output_format
    relative_file = f"generations/{generation.id}/outputs/{candidate_id}.{extension}"
    output_path = safe_project_path(root, relative_file)
    digest = hashlib.sha256(content).hexdigest()
    atomic_write_bytes(output_path, content)
    candidate = Candidate(
        id=candidate_id,
        generation_id=generation.id,
        project_id=generation.project_id,
        file=relative_file,
        sha256=digest,
        width=width,
        height=height,
        mime_type={"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}[extension],
        parent_candidate_id=generation.source_candidate_id,
        created_at=datetime.now(timezone.utc),
    )
    atomic_write_model(root / "candidates" / f"{candidate_id}.json", candidate)
    return candidate


def _write_failure_report(root: Path, generation: Generation) -> Exception | None:
    try:
        from image_workbench.reporting import write_report

        write_report(root, generation)
        return None
    except Exception as exc:
        return exc


def run_generation(
    generation_id: str | UUID,
    *,
    approved_plan_hash: str,
) -> Generation:
    """明示承認された固定計画を実行し、画像と監査情報を保存する。

    この関数を呼ぶのは、CLIが利用者の明示確認を取得した後に限る。
    """
    generation, root = get_generation(generation_id)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise GenerationExecutionError(
            "missing_api_key",
            "OPENAI_API_KEYが設定されていないためAPIを呼び出しませんでした。",
            exit_code=3,
        )
    if approved_plan_hash != generation.plan_hash:
        raise GenerationExecutionError(
            "plan_hash_mismatch",
            "承認した計画hashが一致しません。計画を再表示して確認してください。",
            exit_code=4,
        )

    with ProjectLock(root):
        generation, current_root = get_generation(generation_id)
        if current_root != root:
            raise GenerationExecutionError("project_mismatch", "実験の保存場所が変わりました。", exit_code=4)
        if generation.status not in {"draft", "awaiting_approval"}:
            raise GenerationExecutionError(
                "state_conflict",
                f"この状態の実験は実行できません: {generation.status}",
                exit_code=4,
            )
        verify_plan(generation, root)
        if approved_plan_hash != generation.plan_hash:
            raise GenerationExecutionError(
                "plan_hash_mismatch",
                "プレビュー後に計画が変わりました。APIは呼び出していません。",
                exit_code=4,
            )

        # 送信画像とローカルSDK準備をAPI実行前に完了する。
        image_paths, mask_path = _request_image_paths(generation, root)
        endpoint = (
            "images.generate"
            if generation.operation == "generate" and not image_paths
            else "images.edit"
        )
        from image_workbench.providers.openai_images import (
            OpenAIImagesProvider,
            ProviderFailure,
        )

        provider = OpenAIImagesProvider(api_key)

        now = datetime.now(timezone.utc)
        generation = _generation_with(
            generation,
            status="awaiting_approval",
            approval={"approved_at": now.isoformat(), "plan_hash": approved_plan_hash},
            approved_at=now,
        )
        _save_generation(root, generation)
        _append_event(
            root,
            generation,
            stage="approval",
            event_type="human_approved",
            result="success",
            details={"plan_hash": approved_plan_hash},
        )
        try:
            _append_event(
                root,
                generation,
                stage="api",
                event_type="request_started",
                result="started",
                details={
                    "endpoint": endpoint,
                    "image_count": len(image_paths),
                    "requested_count": generation.requested_count,
                    "model": generation.effective_settings.model,
                },
            )
        except Exception as exc:
            generation = _generation_with(
                generation,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                errors=[{"code": "audit_write_failed", "api_called": False}],
            )
            _save_generation(root, generation)
            raise GenerationExecutionError(
                "audit_write_failed",
                "API実行前に監査イベントを保存できなかったため、APIを呼び出しませんでした。",
                exit_code=6,
            ) from exc
        generation = _generation_with(
            generation,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        _save_generation(root, generation)
        try:
            if endpoint == "images.generate":
                response, attempts = provider.generate(
                    model=generation.effective_settings.model,
                    prompt=generation.final_prompt,
                    count=generation.requested_count,
                    size=generation.effective_settings.size,
                    quality=generation.effective_settings.quality,
                    output_format=generation.effective_settings.output_format,
                )
            else:
                response, attempts = provider.edit(
                    model=generation.effective_settings.model,
                    prompt=generation.final_prompt,
                    count=generation.requested_count,
                    size=generation.effective_settings.size,
                    quality=generation.effective_settings.quality,
                    output_format=generation.effective_settings.output_format,
                    image_paths=image_paths,
                    mask_path=mask_path,
                )
        except ProviderFailure as exc:
            new_errors = [
                *generation.errors,
                {
                    "code": exc.classification,
                    "api_called": True,
                    "outcome": exc.outcome,
                    "status_code": exc.status_code,
                    "request_id": exc.request_id,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
            ]
            failed_slots = (
                list(range(1, generation.requested_count + 1))
                if exc.outcome == "known_failure"
                else generation.failed_slots
            )
            generation = _generation_with(
                generation,
                status="partially_succeeded" if generation.candidate_ids else "failed",
                finished_at=datetime.now(timezone.utc),
                failed_slots=failed_slots,
                attempts=[*generation.attempts, *_safe_attempt_data(list(exc.attempts))],
                errors=new_errors,
                provider_request_ids=list(
                    dict.fromkeys(
                        [
                            *generation.provider_request_ids,
                            *[item.request_id for item in exc.attempts if item.request_id],
                        ]
                    )
                ),
            )
            _save_generation(root, generation)
            try:
                _append_event(
                    root,
                    generation,
                    stage="api",
                    event_type="request_failed",
                    result=exc.classification,
                    details={
                        "outcome": exc.outcome,
                        "status_code": exc.status_code,
                        "request_id": exc.request_id,
                    },
                )
            except Exception as audit_exc:
                generation = _generation_with(
                    generation,
                    errors=[
                        *generation.errors,
                        {"code": "audit_write_failed", "api_called": True,
                         "error_type": type(audit_exc).__name__},
                    ],
                )
                _save_generation(root, generation)
            report_error = _write_failure_report(root, generation)
            message = {
                "network_timeout": "応答待ちがタイムアウトしました。API結果と課金の有無は不明です。自動再送していません。",
                "network_unknown_outcome": "接続が切れ、API結果と課金の有無は不明です。自動再送していません。",
                "provider_server_error": "APIサーバーエラーです。結果が不明なため自動再送していません。",
                "rate_limited": "HTTP 429の再試行を終了しました。保存済み候補を確認してください。",
                "authentication_failed": "API認証またはOrganization権限で拒否されました。APIキー設定と利用権限を確認してください。",
                "organization_verification_required": "Organization verificationが必要です。OpenAI管理画面で確認してください。",
                "content_rejected": "指示または画像がコンテンツ審査で拒否されました。内容を見直してください。",
            }.get(exc.classification, "Images APIが要求を拒否しました。設定と権限を確認してください。")
            if report_error is not None:
                generation = _generation_with(
                    generation,
                    errors=[
                        *generation.errors,
                        {"code": "report_write_failed", "error_type": type(report_error).__name__},
                    ],
                )
                _save_generation(root, generation)
                message += " レポートの保存にも失敗しました。"
            raise GenerationExecutionError(exc.classification, message, exit_code=5) from exc

        except KeyboardInterrupt as exc:
            generation = _generation_with(
                generation,
                status="partially_succeeded" if generation.candidate_ids else "failed",
                finished_at=datetime.now(timezone.utc),
                errors=[
                    *generation.errors,
                    {"code": "user_cancelled", "api_called": "unknown", "outcome": "unknown"},
                ],
            )
            _save_generation(root, generation)
            try:
                _append_event(
                    root, generation, stage="execution", event_type="user_cancelled",
                    result="unknown_outcome", details={},
                )
            except Exception as audit_exc:
                generation = _generation_with(
                    generation,
                    errors=[
                        *generation.errors,
                        {"code": "audit_write_failed", "api_called": "unknown",
                         "error_type": type(audit_exc).__name__},
                    ],
                )
                _save_generation(root, generation)
            report_error = _write_failure_report(root, generation)
            report_notice = ""
            if report_error is not None:
                generation = _generation_with(
                    generation,
                    errors=[
                        *generation.errors,
                        {"code": "report_write_failed", "error_type": type(report_error).__name__},
                    ],
                )
                _save_generation(root, generation)
                report_notice = " レポートの保存にも失敗しました。"
            raise GenerationExecutionError(
                "user_cancelled",
                "実行を中断しました。API結果や課金が不明な可能性があり、自動再送していません。" + report_notice,
                exit_code=130,
            ) from exc
        except (OSError, ValueError) as exc:
            generation = _generation_with(
                generation,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                errors=[
                    *generation.errors,
                    {
                        "code": "request_preparation_failed",
                        "api_called": False,
                        "error_type": type(exc).__name__,
                    },
                ],
            )
            _save_generation(root, generation)
            report_error = _write_failure_report(root, generation)
            report_notice = ""
            if report_error is not None:
                generation = _generation_with(
                    generation,
                    errors=[
                        *generation.errors,
                        {"code": "report_write_failed", "error_type": type(report_error).__name__},
                    ],
                )
                _save_generation(root, generation)
                report_notice = " レポートの保存にも失敗しました。"
            raise GenerationExecutionError(
                "request_preparation_failed",
                "API送信前の画像準備に失敗しました。APIは呼び出していません。" + report_notice,
                exit_code=6,
            ) from exc

        request_id = getattr(response, "_request_id", None)
        data = list(getattr(response, "data", []) or [])
        generation = _generation_with(
            generation,
            attempts=[*generation.attempts, *_safe_attempt_data(list(attempts))],
            provider_request_ids=list(
                dict.fromkeys(
                    [
                        *generation.provider_request_ids,
                        *[item.request_id for item in attempts if item.request_id],
                        *([request_id] if request_id else []),
                    ]
                )
            ),
        )
        _save_generation(root, generation)
        try:
            _append_event(
                root,
                generation,
                stage="api",
                event_type="response_received",
                result="success",
                details={"request_id": request_id, "image_count": len(data)},
            )
        except Exception as exc:
            generation = _generation_with(
                generation,
                errors=[
                    *generation.errors,
                    {"code": "audit_write_failed", "api_called": True, "request_id": request_id},
                ],
            )
            _save_generation(root, generation)

        saved_candidates: list[Candidate] = []
        failed_slots = set(generation.failed_slots)
        errors = list(generation.errors)
        for slot, image_data in enumerate(data[: generation.requested_count], start=1):
            try:
                candidate = _record_candidate(
                    generation,
                    root,
                    slot=slot,
                    image_data=image_data,
                )
                saved_candidates.append(candidate)
                generation = _generation_with(
                    generation,
                    candidate_ids=[*generation.candidate_ids, candidate.id],
                )
                _save_generation(root, generation)
                try:
                    _append_event(
                        root,
                        generation,
                        stage="storage",
                        event_type="candidate_saved",
                        result="success",
                        details={
                            "slot": slot,
                            "candidate_id": str(candidate.id),
                            "file": candidate.file,
                            "sha256": candidate.sha256,
                        },
                    )
                except Exception:
                    errors.append(
                        {"code": "audit_write_failed", "api_called": True, "candidate_id": str(candidate.id)}
                    )
                    generation = _generation_with(generation, errors=errors)
                    _save_generation(root, generation)
            except Exception as exc:
                known_failure = isinstance(exc, ValueError)
                if known_failure:
                    failed_slots.add(slot)
                errors.append(
                    {
                        "code": "candidate_validation_failed" if known_failure else "candidate_save_failed",
                        "api_called": True,
                        "outcome": "known_failure" if known_failure else "unknown",
                        "slot": slot,
                        "error_type": type(exc).__name__,
                    }
                )
                generation = _generation_with(
                    generation,
                    failed_slots=sorted(failed_slots),
                    errors=errors,
                )
                _save_generation(root, generation)

        for missing_slot in range(len(data) + 1, generation.requested_count + 1):
            failed_slots.add(missing_slot)
            errors.append(
                {
                    "code": "provider_returned_fewer_images",
                    "api_called": True,
                    "outcome": "known_failure",
                    "slot": missing_slot,
                }
            )
        if len(data) > generation.requested_count:
            errors.append(
                {
                    "code": "provider_returned_extra_images",
                    "api_called": True,
                    "outcome": "unknown",
                    "extra_count": len(data) - generation.requested_count,
                }
            )

        final_status = (
            "succeeded"
            if len(saved_candidates) == generation.requested_count and not failed_slots
            else "partially_succeeded"
            if saved_candidates
            else "failed"
        )
        generation = _generation_with(
            generation,
            status=final_status,
            finished_at=datetime.now(timezone.utc),
            failed_slots=sorted(failed_slots),
            errors=errors,
        )
        _save_generation(root, generation)
        try:
            _append_event(
                root,
                generation,
                stage="execution",
                event_type="generation_finished",
                result=final_status,
                details={
                    "requested_count": generation.requested_count,
                    "saved_count": len(generation.candidate_ids),
                    "failed_slots": generation.failed_slots,
                    "request_id": request_id,
                },
            )
        except Exception:
            generation = _generation_with(
                generation,
                errors=[*generation.errors, {"code": "audit_write_failed", "api_called": True}],
            )
            _save_generation(root, generation)

        try:
            from image_workbench.reporting import write_report

            write_report(root, generation)
        except Exception as exc:
            generation = _generation_with(
                generation,
                errors=[
                    *generation.errors,
                    {"code": "report_write_failed", "error_type": type(exc).__name__},
                ],
            )
            _save_generation(root, generation)
            raise GenerationExecutionError(
                "report_write_failed",
                "レポートを書き込めませんでした。生成画像と候補データは保存済みです。",
                exit_code=6,
            ) from exc

        if final_status != "succeeded":
            raise GenerationExecutionError(
                "partial_generation",
                f"実験は{final_status}です。保存済み候補と失敗枠を確認してください。",
                exit_code=5,
            )
        return generation
