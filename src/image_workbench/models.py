"""仕様書のJSONデータ構造と値域を表すPydanticモデル。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, UUID4


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("日時にはUTCオフセットが必要です。")
    return value.astimezone(timezone.utc)


def _require_project_relative_posix_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("保存パスは案件ルート相対のPOSIX形式で指定してください。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("保存パスは案件ルート内の相対パスで指定してください。")
    return value


UTCDateTime = Annotated[datetime, AfterValidator(_require_utc)]
ProjectRelativePath = Annotated[str, AfterValidator(_require_project_relative_posix_path)]


class DataModel(BaseModel):
    """余分なキーを拒否し、作成後の変更を許可しないデータモデル。"""
    model_config = ConfigDict(extra="forbid", frozen=True)


class Record(DataModel):
    """JSONファイルとして保存するレコード共通のスキーマ制約。"""
    schema_version: Literal[1] = 1


class Project(Record):
    id: UUID4
    slug: str = Field(pattern=r"^project-[0-9a-f]{8}$")
    name: str
    goal: str
    status: Literal["active", "archived"]
    created_at: UTCDateTime
    updated_at: UTCDateTime
    approved_candidate_id: UUID4 | None = None


class Reference(Record):
    id: UUID4
    file: ProjectRelativePath
    original_filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    role: Literal[
        "pose", "silhouette", "design", "face", "color",
        "orthographic", "part", "style", "other",
    ]
    description: str
    priority: int = Field(default=3, ge=1, le=5)
    created_at: UTCDateTime


class ReferenceHistory(Record):
    id: UUID4
    reference_id: UUID4
    timestamp: UTCDateTime
    before: dict[str, Any]
    after: dict[str, Any]
    reason: str


class Requirement(Record):
    id: UUID4
    description: str
    category: Literal["must_keep", "must_change", "improve"]
    priority: int = Field(default=3, ge=1, le=5)
    status: Literal["active", "retired"] = "active"
    created_at: UTCDateTime
    updated_at: UTCDateTime


class RequirementHistory(Record):
    id: UUID4
    requirement_id: UUID4
    timestamp: UTCDateTime
    action: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str


class RequirementStore(Record):
    """案件の現行要件。変更履歴は別の追記専用レコードとして保存する。"""

    requirements: list[Requirement] = Field(default_factory=list)


class ReferenceIndex(Record):
    """案件内の参照画像レコード一覧。"""

    references: list[Reference] = Field(default_factory=list)


class ReferenceSnapshot(DataModel):
    id: UUID4
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: str
    description: str
    priority: int = Field(ge=1, le=5)
    submitted: bool


class RequirementSnapshot(DataModel):
    id: UUID4
    description: str
    category: Literal["must_keep", "must_change", "improve"]
    priority: int = Field(ge=1, le=5)


class Generation(Record):
    id: UUID4
    project_id: UUID4
    operation: Literal["generate", "edit"]
    edit_mode: Literal["none", "whole"]
    status: Literal[
        "draft", "awaiting_approval", "running", "succeeded",
        "partially_succeeded", "failed", "blocked", "result_unknown", "cancelled",
    ]
    original_instruction: str
    codex_instruction: str
    reference_snapshots: list[ReferenceSnapshot]
    requirements_snapshot: list[RequirementSnapshot]
    source_candidate_id: UUID4 | None = None
    source_candidate_snapshot: dict[str, Any] | None = None
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_count_hint: int = Field(ge=1, le=10)
    requested_count_source: Literal["command_line", "config.toml", "built_in"]
    actual_output_count: int | None = Field(default=None, ge=0)
    registered_candidate_count: int = Field(default=0, ge=0)
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    approval: dict[str, Any] | None = None
    candidate_ids: list[UUID4] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    created_at: UTCDateTime
    approved_at: UTCDateTime | None = None
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None


class CandidateStatusChange(DataModel):
    status: Literal["pending", "selected", "rejected", "on_hold"]
    timestamp: UTCDateTime
    reason: str | None = None


class Candidate(Record):
    id: UUID4
    generation_id: UUID4
    project_id: UUID4
    file: ProjectRelativePath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    parent_candidate_id: UUID4 | None = None
    created_at: UTCDateTime
    status: Literal["pending", "selected", "rejected", "on_hold"] = "pending"
    status_history: list[CandidateStatusChange] = Field(default_factory=list)
    review_ids: list[UUID4] = Field(default_factory=list)


class RequirementResult(DataModel):
    requirement_id: UUID4
    result: Literal["pass", "fail", "uncertain", "not_applicable"]
    comment: str


class Review(Record):
    id: UUID4
    candidate_id: UUID4
    requirement_results: list[RequirementResult]
    issues: list[str]
    rationale: str
    reviewer: Literal["human"] = "human"
    created_at: UTCDateTime


class ExecutionEvent(Record):
    generation_id: UUID4
    sequence: int = Field(ge=1)
    timestamp: UTCDateTime
    stage: str
    event_type: str
    result: str
    details: dict[str, Any] = Field(default_factory=dict)


class Report(Record):
    generation: Generation
    candidates: list[Candidate]
    reviews: list[Review]
    events: list[ExecutionEvent]
    generated_at: UTCDateTime
    summary: dict[str, Any]
