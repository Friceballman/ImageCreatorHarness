"""Codex会話での生成・編集履歴をMarkdown/JSONへまとめる。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from image_workbench.models import Candidate, ExecutionEvent, Generation, Report, Review
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
    summary: dict[str, Any] = {
        "requested_count_hint": generation.requested_count_hint,
        "actual_output_count": generation.actual_output_count,
        "registered_candidate_count": generation.registered_candidate_count,
        "final_status": generation.status,
        "remaining_work": _remaining_work(generation, candidates),
        "next_action": _next_action(generation, candidates),
    }
    return Report(
        generation=generation,
        candidates=candidates,
        reviews=reviews,
        events=events,
        generated_at=datetime.now(timezone.utc),
        summary=summary,
    )


def _remaining_work(generation: Generation, candidates: list[Candidate]) -> list[str]:
    command = "generate" if generation.operation == "generate" else "edit"
    if generation.status == "blocked":
        return ["利用枠または画像生成機能の状態を確認してください。"]
    if generation.status == "result_unknown":
        return ["生成結果と利用枠を確認するまで再生成しないでください。確認後、結果に応じて画像登録または新しい生成計画を作成してください。"]
    if generation.status == "draft":
        return [f"Codex会話で生成計画を提示し、iwb {command} request-approvalで承認待ちを記録してください。"]
    if generation.status == "awaiting_approval":
        return [f"利用者の明示承認を待ち、承認後にiwb {command} approveを記録してください。"]
    if generation.status == "running":
        return ["Codex会話の生成結果を確認し、画像ファイルを登録してください。"]
    if not candidates:
        return ["生成結果は候補として登録されていません。原因と会話結果を確認してください。"]
    pending = [candidate for candidate in candidates if candidate.status == "pending"]
    return [f"候補 {candidate.id} をレビューしてください。" for candidate in pending]


def _next_action(generation: Generation, candidates: list[Candidate]) -> str:
    command = "generate" if generation.operation == "generate" else "edit"
    if generation.status == "blocked":
        return "利用枠回復後に利用者が新しい生成計画を依頼した場合のみ再開してください。"
    if generation.status == "result_unknown":
        return "生成結果を確認して状態を確定してください。自動再生成はしません。"
    if generation.status == "draft":
        return f"Codex会話で生成計画を提示し、iwb {command} request-approvalを記録してください。"
    if generation.status == "awaiting_approval":
        return f"利用者の明示承認を待ち、承認後にiwb {command} approveを記録してください。"
    if generation.status == "running":
        return "Codex会話から得られた画像を検証し、iwbで登録してください。"
    if candidates:
        return "候補を比較し、レビューと採用判断を記録してください。"
    return "生成結果とエラー内容を確認し、必要なら新しい生成計画を作成してください。"


def render_markdown(report: Report) -> str:
    generation = report.generation
    lines = [
        f"# 生成計画レポート: {generation.id}",
        "",
        "## 概要",
        "",
        f"- 案件ID: `{generation.project_id}`",
        f"- 操作: `{generation.operation}` / `{generation.edit_mode}`",
        f"- 状態: `{generation.status}`",
        f"- 作成日時: {generation.created_at.isoformat()}",
        f"- 承認日時: {generation.approved_at.isoformat() if generation.approved_at else '未記録'}",
        f"- 生成開始日時: {generation.started_at.isoformat() if generation.started_at else '未記録'}",
        f"- 希望枚数（目安）: {generation.requested_count_hint}",
        f"- 実出力数: {generation.actual_output_count if generation.actual_output_count is not None else '未取得'}",
        f"- 登録候補数: {generation.registered_candidate_count}",
        "- 画像生成経路: Codex会話内の組み込み機能",
        "",
        "## 指示",
        "",
        "### 元指示",
        "",
        generation.original_instruction,
        "",
        "### Codexへ渡す指示",
        "",
        generation.codex_instruction,
        "",
        "## 参照画像",
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
            f"- `{reference.id}` ({reference.role}, 優先度 {reference.priority}, "
            f"使用{'あり' if reference.submitted else 'なし'}): {reference.description} "
            f"/ SHA-256 `{reference.sha256}`"
        )
    if not generation.reference_snapshots and not generation.source_candidate_id:
        lines.append("参照画像はありません。")

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
        lines.append("候補は登録されていません。")

    lines.extend(["## レビュー", ""])
    if report.reviews:
        for review in report.reviews:
            lines.append(f"- `{review.candidate_id}` / {review.created_at.isoformat()} / {review.rationale}")
            for result in review.requirement_results:
                lines.append(f"  - 要件 `{result.requirement_id}`: `{result.result}` — {result.comment}")
    else:
        lines.append("レビューはありません。")

    lines.extend(["", "## 会話イベント", ""])
    for event in report.events:
        lines.append(
            f"- {event.timestamp.isoformat()} / `{event.stage}` / "
            f"`{event.event_type}` / `{event.result}` / `{event.details}`"
        )
    lines.extend(
        [
            "",
            "## 残作業と次の操作",
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
    atomic_write_bytes(
        report_dir / f"{generation.id}.md",
        render_markdown(report).encode("utf-8"),
        replace=True,
    )
    return report


def read_report(project_root: Path, generation_id: str) -> Report:
    return read_model(project_root / "reports" / f"{generation_id}.json", Report)
