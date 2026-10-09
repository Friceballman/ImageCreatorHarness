"""生成・編集実験のMarkdown/JSON監査レポート。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from image_workbench.models import (
    Candidate,
    ExecutionEvent,
    Generation,
    Report,
    Review,
)
from image_workbench.storage import atomic_write_bytes, atomic_write_model, read_model


def _load_report_parts(project_root: Path, generation: Generation) -> Report:
    candidates: list[Candidate] = []
    reviews: list[Review] = []
    for candidate_id in generation.candidate_ids:
        candidate_path = project_root / "candidates" / f"{candidate_id}.json"
        candidate = read_model(candidate_path, Candidate)
        candidates.append(candidate)
        for review_id in candidate.review_ids:
            review_path = project_root / "reviews" / f"{review_id}.json"
            reviews.append(read_model(review_path, Review))

    event_dir = project_root / "generations" / str(generation.id) / "events"
    events = [read_model(path, ExecutionEvent) for path in sorted(event_dir.glob("*.json"))]
    saved_count = len(candidates)
    summary: dict[str, Any] = {
        "requested_count": generation.requested_count,
        "saved_count": saved_count,
        "failed_count": len(generation.failed_slots),
        "api_attempt_count": len(generation.attempts),
        "final_status": generation.status,
        "remaining_work": [
            f"候補枠{slot}の結果を確認してください。" for slot in generation.failed_slots
        ],
        "next_action": (
            "候補を比較し、レビューと評価を記録してください。"
            if candidates else "失敗理由を確認し、必要なら再計画してください。"
        ),
    }
    return Report(
        generation=generation,
        candidates=candidates,
        reviews=reviews,
        events=events,
        generated_at=datetime.now(timezone.utc),
        summary=summary,
    )


def render_markdown(report: Report) -> str:
    generation = report.generation
    lines = [
        f"# 画像制作実験レポート: {generation.id}",
        "",
        "## 概要",
        "",
        f"- 案件ID: `{generation.project_id}`",
        f"- 操作: `{generation.operation}` / `{generation.edit_mode}`",
        f"- 状態: `{generation.status}`",
        f"- 作成日時: {generation.created_at.isoformat()}",
        f"- 承認日時: {generation.approved_at.isoformat() if generation.approved_at else '未承認'}",
        f"- 要求枚数: {generation.requested_count}",
        f"- 保存枚数: {len(report.candidates)}",
        f"- 失敗枠数: {len(generation.failed_slots)}",
        f"- API試行数: {len(generation.attempts)}",
        f"- Provider request ID: {', '.join(generation.provider_request_ids) or '取得不可'}",
        f"- 費用見積り: {generation.cost_estimate.get('display', '取得不可')}",
        "",
        "## 指示と設定",
        "",
        "### 元指示",
        "",
        generation.original_instruction,
        "",
        "### 最終プロンプト",
        "",
        generation.final_prompt,
        "",
        "### 実効設定",
        "",
        f"- モデル: `{generation.effective_settings.model}`",
        f"- サイズ: `{generation.effective_settings.size}`",
        f"- 品質: `{generation.effective_settings.quality}`",
        f"- 出力形式: `{generation.effective_settings.output_format}`",
        f"- 設定の出所: `{generation.effective_settings.sources}`",
        "",
        "## 送信画像",
        "",
    ]
    if generation.source_candidate_snapshot:
        source = generation.source_candidate_snapshot
        lines.append(
            f"- 編集元候補 `{generation.source_candidate_id}`: `{source.get('file')}`, "
            f"SHA-256 `{source.get('sha256')}`"
        )
    for reference in generation.reference_snapshots:
        lines.append(
            f"- 参照 `{reference.id}` ({reference.role}, 優先度 {reference.priority}): "
            f"{reference.description} / SHA-256 `{reference.sha256}`"
        )
    if generation.mask_snapshot:
        lines.append(
            f"- マスク: `{generation.mask_snapshot.get('file')}`, "
            f"SHA-256 `{generation.mask_snapshot.get('sha256')}`"
        )
    if not generation.reference_snapshots and not generation.source_candidate_id:
        lines.append("- 送信画像なし")

    lines.extend(["", "## 要件スナップショット", ""])
    if generation.requirements_snapshot:
        for requirement in generation.requirements_snapshot:
            lines.append(
                f"- `{requirement.id}` ({requirement.category}, 優先度 {requirement.priority}): "
                f"{requirement.description}"
            )
    else:
        lines.append("要件はありません。")

    lines.extend(["", "## 候補", ""])
    if report.candidates:
        for candidate in report.candidates:
            lines.extend(
                [
                    f"### {candidate.id}",
                    "",
                    f"- 状態: `{candidate.status}`",
                    f"- 保存先: `{candidate.file}`",
                    f"- SHA-256: `{candidate.sha256}`",
                    f"- 寸法: {candidate.width} × {candidate.height}",
                    f"- 親候補: `{candidate.parent_candidate_id or 'なし'}`",
                    f"- レビューID: {', '.join(str(item) for item in candidate.review_ids) or 'なし'}",
                    "",
                ]
            )
    else:
        lines.append("候補は保存されていません。")

    lines.extend(["## レビュー", ""])
    if report.reviews:
        for review in report.reviews:
            lines.append(
                f"- `{review.candidate_id}` / {review.created_at.isoformat()} / "
                f"{review.rationale}"
            )
            for result in review.requirement_results:
                lines.append(
                    f"  - 要件 `{result.requirement_id}`: `{result.result}` — {result.comment}"
                )
    else:
        lines.append("レビューはありません。")

    lines.extend(["", "## 試行とイベント", ""])
    for attempt in generation.attempts:
        lines.append(f"- 試行: `{attempt}`")
    for event in report.events:
        lines.append(
            f"- {event.timestamp.isoformat()} / `{event.stage}` / "
            f"`{event.event_type}` / `{event.result}` / `{event.details}`"
        )
    lines.extend(
        [
            "",
            "## エラーと次の操作",
            "",
            f"- 残作業: {report.summary['remaining_work'] or 'なし'}",
            f"- 次の操作: {report.summary['next_action']}",
        ]
    )
    for error in generation.errors:
        lines.append(f"- エラー記録: `{error}`")
    return "\n".join(lines) + "\n"


def write_report(project_root: Path, generation: Generation) -> Report:
    """監査JSONとMarkdownを作成または更新する。"""
    report = _load_report_parts(project_root, generation)
    report_dir = project_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_model(report_dir / f"{generation.id}.json", report, replace=True)
    markdown = render_markdown(report).encode("utf-8")
    atomic_write_bytes(report_dir / f"{generation.id}.md", markdown, replace=True)
    return report


def read_report(project_root: Path, generation_id: str) -> Report:
    return read_model(project_root / "reports" / f"{generation_id}.json", Report)
