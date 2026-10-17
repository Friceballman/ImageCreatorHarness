"""iwbコマンドの初期CLI。"""

from __future__ import annotations

import typer
from pathlib import Path
import os
import subprocess
import sys
from rich.console import Console
from rich.table import Table
from typing import Literal
from uuid import uuid4

from image_workbench.config import ConfigError, ResolvedConfig, load_config
from image_workbench.exports import (
    ExportNameError, export_filenames, export_selected_candidates, selected_candidates,
    validate_export_name,
)
from image_workbench.ids import parse_uuid4
from image_workbench.list_view import cache_root, clear_caches, list_caches, show_listing
from image_workbench.projects import (
    archive_project,
    create_project,
    get_project,
    list_projects,
    ProjectStateConflict,
    project_root,
)
from image_workbench.storage import (
    LockConflict,
    StorageError,
    read_model,
    safe_project_path,
    sha256_file,
)
from image_workbench.requirements_store import (
    add_requirement,
    list_requirements,
    requirement_history,
    retire_requirement,
    update_requirement,
)
from image_workbench.models import Candidate, Generation
from image_workbench.references import (
    DuplicateReferenceError,
    InvalidImageError,
    add_reference,
    find_duplicate_reference,
    list_references,
    show_reference,
    update_reference,
)
from image_workbench.evaluation import (
    CandidateNotFound,
    CandidateStateConflict,
    ReviewRequired,
    find_candidate,
    list_candidates,
    review_candidate,
    set_approved_candidate,
    update_candidate_status,
)
from image_workbench.models import RequirementResult
from image_workbench.reporting import read_report, render_markdown, write_report
from image_workbench.generations import (
    GenerationExecutionError,
    GenerationPlanError,
    approve_generation,
    block_generation,
    cancel_generation,
    fail_generation,
    get_generation,
    list_generations,
    mark_generation_unknown,
    plan_edit,
    plan_generate,
    register_generation_files,
    request_generation_approval,
    verify_plan,
)


app = typer.Typer(
    name="iwb",
    help="ローカル画像制作ハーネスを操作します。",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(
    help="設定を確認します。",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(
    help="案件を管理します。",
    no_args_is_help=True,
    add_completion=False,
)
requirement_app = typer.Typer(
    help="案件の制作要件と変更履歴を管理します。",
    no_args_is_help=True,
    add_completion=False,
)
reference_app = typer.Typer(
    help="案件の参照画像を管理します。",
    no_args_is_help=True,
    add_completion=False,
)
generate_app = typer.Typer(
    help="生成計画を管理します。",
    no_args_is_help=True,
    add_completion=False,
)
edit_app = typer.Typer(
    help="候補画像を編集する生成計画を管理します。",
    no_args_is_help=True,
    add_completion=False,
)
candidate_app = typer.Typer(
    help="生成候補を比較・レビュー・評価します。",
    no_args_is_help=True,
    add_completion=False,
)
report_app = typer.Typer(
    help="生成計画レポートを表示または開きます。",
    no_args_is_help=True,
    add_completion=False,
)
cache_app = typer.Typer(
    help="一覧表示用のキャッシュを管理します。",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
app.add_typer(config_app, name="config")
app.add_typer(project_app, name="project")
app.add_typer(requirement_app, name="requirement")
app.add_typer(reference_app, name="reference")
app.add_typer(generate_app, name="generate")
app.add_typer(edit_app, name="edit")
app.add_typer(candidate_app, name="candidate")
app.add_typer(report_app, name="report")
app.add_typer(cache_app, name="cache")


def _config_rows(config: ResolvedConfig) -> list[tuple[str, str, str]]:
    values = config.values
    return [
        (
            "default_candidate_count",
            str(values.default_candidate_count),
            config.sources["default_candidate_count"],
        ),
        (
            "max_candidate_count",
            str(values.max_candidate_count),
            config.sources["max_candidate_count"],
        ),
        ("projects_dir", values.projects_dir.as_posix(), config.sources["projects_dir"]),
    ]


@config_app.command("show")
def config_show() -> None:
    """有効な設定値と各値の出所を表示します。"""
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    table = Table(title="有効な設定")
    table.add_column("設定項目", style="cyan")
    table.add_column("値")
    table.add_column("出所", style="dim")
    for key, value, source in _config_rows(config):
        table.add_row(key, value, source)
    console.print(table)


@project_app.command("create")
def project_create(
    name: str = typer.Option(..., "--name", help="案件名"),
    goal: str = typer.Option(..., "--goal", help="案件の目的"),
) -> None:
    """案件と初期データを作成します。"""
    try:
        project = create_project(name, goal)
    except (OSError, StorageError, ValueError) as exc:
        console.print(f"[red]案件を作成できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"案件を作成しました: {project.name} ({project.id})")


def _show_list(title: str, records: list[list[tuple[str, object]]], *, terminal: bool) -> None:
    try:
        show_listing(title, records, terminal=terminal)
    except OSError as exc:
        console.print(f"[red]一覧キャッシュを保存・表示できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc


@cache_app.command("clear")
def cache_clear_command(
    cache_id: str | None = typer.Argument(None, help="削除するキャッシュID（省略時は全一覧キャッシュ）"),
) -> None:
    """一覧キャッシュを確認後に削除します。案件データは削除しません。"""
    try:
        identifier = parse_uuid4(cache_id) if cache_id is not None else None
    except ValueError as exc:
        console.print(f"[red]キャッシュIDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    try:
        records, warnings = list_caches()
        for warning in warnings:
            print(warning, file=sys.stderr)
        if identifier is not None:
            records = [record for record in records if record.id == identifier]
            if not records:
                console.print("[red]指定された有効な一覧キャッシュが見つかりません。[/red]")
                raise typer.Exit(code=4)
        if not records:
            console.print("削除対象の一覧キャッシュは0件です。")
            return
        sys.stdout.write(f"一覧キャッシュの保存先: {cache_root()}\n")
        for record in records:
            sys.stdout.write(f"{record.id} / {record.created_at.isoformat()} / {record.title}\n")
        console.print(f"削除対象: {len(records)}件。VS Codeで表示中のキャッシュも対象になります。")
        if not typer.confirm("これらの一覧キャッシュを削除しますか？", default=False):
            console.print("一覧キャッシュの削除を中止しました。")
            raise typer.Exit(code=130)
        removed, errors = clear_caches([record.id for record in records])
        console.print(f"一覧キャッシュを削除しました: {removed}件")
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            raise typer.Exit(code=6)
    except OSError as exc:
        console.print(f"[red]一覧キャッシュを削除できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc


@project_app.command("list")
def project_list(
    terminal: bool = typer.Option(False, "--terminal", help="エディタを開かず全文を端末へ表示"),
) -> None:
    """案件一覧を表示します。"""
    try:
        projects = list_projects()
    except (OSError, StorageError, ValueError) as exc:
        console.print(f"[red]案件一覧を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    _show_list("案件一覧", [
        [("案件ID", project.id), ("名前", project.name), ("状態", project.status), ("目的", project.goal)]
        for project in projects
    ], terminal=terminal)


@project_app.command("show")
def project_show(project_id: str = typer.Argument(..., help="案件ID（UUID v4）")) -> None:
    """案件の詳細を表示します。"""
    try:
        project = get_project(project_id)
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]案件を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    table = Table(title=f"案件: {project.name}")
    table.add_column("項目", style="cyan")
    table.add_column("値")
    table.add_row("案件ID", str(project.id))
    table.add_row("slug", project.slug)
    table.add_row("状態", project.status)
    table.add_row("目的", project.goal)
    table.add_row("採用版候補ID", str(project.approved_candidate_id or "未設定"))
    table.add_row("作成日時", project.created_at.isoformat())
    table.add_row("更新日時", project.updated_at.isoformat())
    console.print(table)


@project_app.command("archive")
def project_archive(project_id: str = typer.Argument(..., help="案件ID（UUID v4）")) -> None:
    """確認後に案件を読み取り専用へアーカイブします。"""
    try:
        project = get_project(project_id)
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]案件を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    console.print(f"対象案件: {project.name} ({project.id})")
    if project.status != "active":
        console.print("[red]アーカイブ済み案件です。[/red]")
        raise typer.Exit(code=4)
    if not typer.confirm("この案件をアーカイブしますか？"):
        console.print("アーカイブを中止しました。")
        raise typer.Exit(code=130)
    try:
        archive_project(project.id)
    except ProjectStateConflict as exc:
        console.print(f"[red]アーカイブできません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError, ValueError) as exc:
        console.print(f"[red]アーカイブできませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print("案件をアーカイブしました。以後は読み取り専用です。")


@project_app.command("set-approved")
def project_set_approved_command(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    candidate_id: str = typer.Argument(..., help="selected候補ID（UUID v4）"),
) -> None:
    """selected候補を案件の採用版に設定します。"""
    try:
        project = get_project(project_id)
        candidate, candidate_project, _ = find_candidate(candidate_id)
    except ValueError as exc:
        console.print(f"[red]IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    if candidate_project.id != project.id:
        console.print("[red]候補が別案件に属しています。[/red]")
        raise typer.Exit(code=4)
    console.print(f"案件: {project.name} ({project.id})")
    console.print(f"現在の採用版: {project.approved_candidate_id or '未設定'}")
    console.print(f"新しい採用版: {candidate.id} (状態: {candidate.status})")
    unreviewed = not candidate.review_ids
    reason: str | None = None
    if unreviewed:
        console.print("[yellow]未レビュー候補です。[/yellow]")
    if not typer.confirm("この候補を案件の採用版に設定しますか？", default=False):
        console.print("採用版の変更を中止しました。")
        raise typer.Exit(code=130)
    if unreviewed:
        if not typer.confirm("未レビュー候補を採用することを明示承認しますか？", default=False):
            console.print("採用版の変更を中止しました。")
            raise typer.Exit(code=130)
        reason = typer.prompt("未レビュー候補を採用する理由")
    try:
        updated = set_approved_candidate(
            project.id,
            candidate.id,
            allow_unreviewed=unreviewed,
            reason=reason,
        )
    except ReviewRequired as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except CandidateStateConflict as exc:
        console.print(f"[red]採用版を変更できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]入力が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]採用版を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"案件の採用版を更新しました: {updated.approved_candidate_id}")
    _refresh_candidate_report(candidate_id)


@project_app.command("export")
def project_export_command(
    project_id: str = typer.Argument(..., help="出力対象の案件ID（UUID v4）"),
    name: str | None = typer.Option(
        None, "--name", help="出力の基本名（拡張子なし）。1枚は指定名.png、複数は指定名_001.png等。省略時は候補ID。",
    ),
) -> None:
    """selected候補すべてを出力します。例: --name 'ロボット立ち絵'（ver4.3）。"""
    try:
        normalized_id = parse_uuid4(project_id)
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    try:
        if name is not None:
            validate_export_name(name)
        project = get_project(normalized_id)
        candidates = selected_candidates(project.id)
        filenames = export_filenames(candidates, name)
        if not candidates:
            typer.echo("selected候補は0件です。出力ファイルは作成しませんでした。")
            return
        export_id = uuid4()
        output = project_root(project.id) / "exports" / str(export_id)
        typer.echo(f"対象案件: {project.name} ({project.id})")
        typer.echo(f"出力対象: selected候補 {len(candidates)}件")
        for candidate, filename in zip(candidates, filenames, strict=True):
            typer.echo(f"  {candidate.id} → {filename} / {candidate.width}×{candidate.height}")
        typer.echo(f"出力先: {output}")
        if not typer.confirm("これらの画像を出力しますか？", default=False):
            console.print("画像の出力を中止しました。")
            raise typer.Exit(code=130)
        record = export_selected_candidates(
            project.id, expected_candidates=candidates, name=name, export_id=export_id,
        )
    except ExportNameError as exc:
        typer.echo(f"出力名が不正です: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except ProjectStateConflict as exc:
        console.print(f"[red]画像を出力できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]入力または画像の検証に失敗しました:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except OSError as exc:
        console.print(
            f"[red]画像を出力できませんでした:[/red] {exc}。"
            "元画像と過去の出力は保持しています。保存先を確認して再実行してください。"
        )
        raise typer.Exit(code=6) from exc
    if record is not None:
        output = project_root(record.project_id) / "exports" / str(record.id)
        typer.echo(f"画像を出力しました: {len(record.images)}件 / {output}")
        typer.echo(f"画像一覧: {output / 'manifest.json'}")


@requirement_app.command("add")
def requirement_add(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    category: Literal["must_keep", "must_change", "improve"] = typer.Option(
        ..., "--category", help="要件区分"
    ),
    text: str = typer.Option(..., "--text", help="制作要件"),
    priority: int = typer.Option(3, "--priority", min=1, max=5, help="優先度（1が最高）"),
) -> None:
    """案件に制作要件を追加します。"""
    try:
        item = add_requirement(
            project_id, category=category, description=text, priority=priority
        )
    except ProjectStateConflict as exc:
        console.print(f"[red]案件の状態により追加できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]入力または案件状態が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]要件を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"要件を追加しました: {item.id}")


@requirement_app.command("list")
def requirement_list(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    terminal: bool = typer.Option(False, "--terminal", help="エディタを開かず全文を端末へ表示"),
) -> None:
    """有効な制作要件を一覧表示します。"""
    try:
        items = list_requirements(project_id)
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]要件一覧を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    _show_list(f"有効な制作要件 / 案件ID: {project_id}", [
        [("要件ID", item.id), ("区分", item.category), ("優先度", item.priority), ("要件", item.description)]
        for item in items
    ], terminal=terminal)


@requirement_app.command("update")
def requirement_update(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    requirement_id: str = typer.Argument(..., help="要件ID（UUID v4）"),
    category: Literal["must_keep", "must_change", "improve"] | None = typer.Option(
        None, "--category", help="変更後の要件区分"
    ),
    text: str | None = typer.Option(None, "--text", help="変更後の要件"),
    priority: int | None = typer.Option(
        None, "--priority", min=1, max=5, help="変更後の優先度"
    ),
    reason: str = typer.Option(..., "--reason", help="変更理由"),
) -> None:
    """要件を変更し、変更前後と理由を履歴に保存します。"""
    try:
        item = update_requirement(
            project_id,
            requirement_id,
            category=category,
            description=text,
            priority=priority,
            reason=reason,
        )
    except ProjectStateConflict as exc:
        console.print(f"[red]案件の状態により更新できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]入力または案件状態が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]要件を更新できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"要件を更新しました: {item.id}")


@requirement_app.command("retire")
def requirement_retire(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    requirement_id: str = typer.Argument(..., help="要件ID（UUID v4）"),
    reason: str = typer.Option(..., "--reason", help="廃止理由"),
) -> None:
    """要件を廃止し、廃止理由を履歴に保存します。"""
    try:
        item = retire_requirement(project_id, requirement_id, reason=reason)
    except ProjectStateConflict as exc:
        console.print(f"[red]案件の状態により廃止できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]入力または案件状態が不正です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]要件を廃止できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"要件を廃止しました: {item.id}")


@requirement_app.command("history")
def requirement_history_show(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    requirement_id: str | None = typer.Argument(None, help="要件ID（省略時は全履歴）"),
) -> None:
    """要件の変更履歴を表示します。"""
    try:
        records = requirement_history(project_id, requirement_id)
    except ValueError as exc:
        console.print(f"[red]IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]要件履歴を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    table = Table(title="要件変更履歴")
    table.add_column("日時")
    table.add_column("要件ID", style="cyan")
    table.add_column("操作")
    table.add_column("理由")
    table.add_column("変更前")
    table.add_column("変更後")
    for event in records:
        table.add_row(
            event.timestamp.isoformat(),
            str(event.requirement_id),
            event.action,
            event.reason,
            str(event.before),
            str(event.after),
        )
    console.print(table)


@reference_app.command("add")
def reference_add(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    image_path: Path = typer.Argument(..., help="登録するPNG/JPEG/WebP画像"),
    role: Literal[
        "pose", "silhouette", "design", "face", "color",
        "orthographic", "part", "style", "other",
    ] = typer.Option(..., "--role", help="参照画像の役割"),
    description: str = typer.Option(..., "--description", help="参照画像の説明"),
    priority: int = typer.Option(3, "--priority", min=1, max=5, help="優先度（1が最高）"),
) -> None:
    """画像を検証し、案件へ参照として取り込みます。"""
    duplicate_action: Literal["reuse", "share"] | None = None
    try:
        duplicate = find_duplicate_reference(project_id, image_path)
        if duplicate is not None:
            console.print(
                f"同一画像が登録済みです: {duplicate.id} "
                f"(役割: {duplicate.role}, ファイル: {duplicate.file})"
            )
            if typer.confirm("既存の参照レコードを再利用しますか？", default=True):
                duplicate_action = "reuse"
            elif typer.confirm("同じ画像ファイルを共有する別レコードを追加しますか？"):
                duplicate_action = "share"
            else:
                console.print("参照画像の登録を中止しました。")
                raise typer.Exit(code=130)
        reference = add_reference(
            project_id,
            image_path,
            role=role,
            description=description,
            priority=priority,
            duplicate_action=duplicate_action,
        )
    except ProjectStateConflict as exc:
        console.print(f"[red]案件の状態により登録できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except DuplicateReferenceError as exc:
        console.print(f"[yellow]登録中に同一画像が見つかりました。再実行して選択してください:[/yellow] {exc}")
        raise typer.Exit(code=4) from exc
    except InvalidImageError as exc:
        console.print(f"[red]参照画像が不正です:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except ValueError as exc:
        console.print(f"[red]入力または案件状態が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]参照画像を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"参照画像を登録しました: {reference.id}")


@reference_app.command("list")
def reference_list(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    terminal: bool = typer.Option(False, "--terminal", help="エディタを開かず全文を端末へ表示"),
) -> None:
    """案件の参照画像一覧を表示します。"""
    try:
        references = list_references(project_id)
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]参照画像一覧を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    _show_list(f"参照画像一覧 / 案件ID: {project_id}", [
        [("参照ID", reference.id), ("役割", reference.role), ("優先度", reference.priority),
         ("説明", reference.description), ("ファイル", reference.file)]
        for reference in references
    ], terminal=terminal)


@reference_app.command("show")
def reference_show(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    reference_id: str = typer.Argument(..., help="参照ID（UUID v4）"),
) -> None:
    """参照画像の詳細を表示します。"""
    try:
        reference = show_reference(project_id, reference_id)
    except ValueError as exc:
        console.print(f"[red]IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]参照画像を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    table = Table(title=f"参照画像: {reference.id}")
    table.add_column("項目", style="cyan")
    table.add_column("値")
    table.add_row("ファイル", reference.file)
    table.add_row("元ファイル名", reference.original_filename)
    table.add_row("SHA-256", reference.sha256)
    table.add_row("MIME type", reference.mime_type)
    table.add_row("役割", reference.role)
    table.add_row("説明", reference.description)
    table.add_row("優先度", str(reference.priority))
    table.add_row("登録日時", reference.created_at.isoformat())
    console.print(table)


@reference_app.command("update")
def reference_update(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    reference_id: str = typer.Argument(..., help="参照ID（UUID v4）"),
    role: Literal[
        "pose", "silhouette", "design", "face", "color",
        "orthographic", "part", "style", "other",
    ] | None = typer.Option(None, "--role", help="変更後の役割"),
    description: str | None = typer.Option(None, "--description", help="変更後の説明"),
    priority: int | None = typer.Option(
        None, "--priority", min=1, max=5, help="変更後の優先度"
    ),
    reason: str = typer.Option(..., "--reason", help="変更理由"),
) -> None:
    """参照画像のメタデータを変更履歴付きで更新します。"""
    try:
        reference = update_reference(
            project_id,
            reference_id,
            role=role,
            description=description,
            priority=priority,
            reason=reason,
        )
    except ProjectStateConflict as exc:
        console.print(f"[red]案件の状態により更新できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]入力または案件状態が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]参照画像を更新できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"参照画像の情報を更新しました: {reference.id}")


def _show_generation(generation: Generation) -> None:
    try:
        project = get_project(generation.project_id)
    except (LookupError, OSError, StorageError) as exc:
        console.print(f"[red]案件情報を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    table = Table(title=f"生成計画: {project.name}")
    table.add_column("項目", style="cyan")
    table.add_column("内容")
    table.add_row("生成計画ID", str(generation.id))
    table.add_row("操作", generation.operation)
    table.add_row("状態", generation.status)
    table.add_row("Plan hash", generation.plan_hash)
    table.add_row("承認日時", generation.approved_at.isoformat() if generation.approved_at else "未記録")
    table.add_row("生成開始日時", generation.started_at.isoformat() if generation.started_at else "未記録")
    table.add_row("希望枚数（目安）", str(generation.requested_count_hint))
    table.add_row("希望枚数の出所", generation.requested_count_source)
    table.add_row(
        "実出力数",
        str(generation.actual_output_count)
        if generation.actual_output_count is not None
        else "未取得",
    )
    table.add_row("登録候補数", str(generation.registered_candidate_count))
    table.add_row("生成経路", "Codex会話内の組み込み機能")
    table.add_row("保存先", str(project_root(project.id) / "generations" / str(generation.id)))
    table.add_row("元指示", generation.original_instruction)
    table.add_row("Codexへ渡す指示", generation.codex_instruction)
    if generation.source_candidate_id is not None:
        source = generation.source_candidate_snapshot or {}
        table.add_row(
            "編集元候補",
            f"{generation.source_candidate_id} / {source.get('file', 'パスなし')} / SHA-256 {source.get('sha256', '不明')}",
        )
        table.add_row("編集モード", generation.edit_mode)
    table.add_row("使用参照画像数", str(len(generation.reference_snapshots)))
    if generation.errors:
        table.add_row("停止・失敗記録", str(generation.errors))
    console.print(table)
    if generation.reference_snapshots:
        refs = Table(title="生成計画に含む参照画像")
        refs.add_column("参照ID", style="cyan")
        refs.add_column("役割")
        refs.add_column("優先度")
        refs.add_column("説明")
        refs.add_column("SHA-256")
        refs.add_column("ファイル")
        current_refs = {
            reference.id: reference
            for reference in list_references(generation.project_id)
        }
        for item in generation.reference_snapshots:
            current = current_refs.get(item.id)
            refs.add_row(
                str(item.id), item.role, str(item.priority), item.description,
                item.sha256, current.file if current else "見つかりません",
            )
        console.print(refs)


def _refresh_candidate_report(candidate_id: str) -> None:
    """候補操作後、既に作成済みの生成計画レポートを最新化する。"""
    try:
        candidate, _, root = find_candidate(candidate_id)
        report_path = root / "reports" / f"{candidate.generation_id}.json"
        if report_path.is_file():
            generation, _ = get_generation(candidate.generation_id)
            write_report(root, generation)
    except Exception as exc:
        console.print(
            "[red]操作データは保存済みですが、レポートを更新できませんでした。"
            f"候補ID: {candidate_id} / エラー: {type(exc).__name__}[/red]"
        )


def _register_generation_command(generation_id: str, image_paths: list[Path]) -> None:
    try:
        generation, root = get_generation(generation_id)
        verify_plan(generation, root)
    except ValueError as exc:
        console.print(f"[red]生成計画IDまたは内容が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]生成計画を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    _show_generation(generation)
    console.print("画像生成は行いません。Codex会話から保存した画像ファイルを検証・登録します。")
    if generation.status == "result_unknown" and not typer.confirm(
        "Codex会話でこのファイルが対象生成計画の生成結果だと確認しましたか？", default=False
    ):
        console.print("結果不明のままです。画像登録を中止しました。")
        raise typer.Exit(code=130)
    try:
        completed = register_generation_files(
            generation.id,
            image_paths,
        )
    except GenerationExecutionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]生成計画記録を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(
        f"画像を登録しました: {completed.id} "
        f"(希望: {completed.requested_count_hint}枚 / "
        f"実出力: {completed.actual_output_count or 0}枚 / "
        f"登録候補: {completed.registered_candidate_count}件)"
    )


def _record_generation_action(action: str, generation_id: str, **kwargs: object) -> None:
    operations = {
        "request-approval": request_generation_approval,
        "approve": approve_generation,
        "block": block_generation,
        "mark-unknown": mark_generation_unknown,
        "fail": fail_generation,
        "cancel": cancel_generation,
    }
    operation = operations[action]
    try:
        generation = operation(generation_id, **kwargs)
    except GenerationExecutionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc
    except ValueError as exc:
        console.print(f"[red]入力または生成計画が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]状態と履歴を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    try:
        _, root = get_generation(generation.id)
        write_report(root, generation)
    except Exception as exc:
        console.print(
            "[yellow]状態とイベントは記録済みですが、レポートを更新できませんでした。"
            f"生成計画ID: {generation.id} / エラー: {type(exc).__name__}[/yellow]"
        )

    messages = {
        "request-approval": "生成計画を提示し、承認待ちとして記録しました。",
        "approve": "会話での明示承認と生成開始を記録しました。続けてCodex会話で画像生成を行ってください。",
        "block": "生成開始前の停止を記録しました。",
        "mark-unknown": "結果不明として記録しました。確認が済むまで再生成しないでください。",
        "fail": "生成開始後の明確な失敗を記録しました。",
        "cancel": "承認前の取消を記録しました。",
    }
    console.print(f"{messages[action]} 生成計画ID: {generation.id} / 状態: {generation.status}")


def _add_generation_lifecycle_commands(group: typer.Typer) -> None:
    """generate/edit共通の会話状態記録コマンドを登録する。"""
    def request_approval(generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）")) -> None:
        _record_generation_action("request-approval", generation_id)

    def approve(generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）")) -> None:
        _record_generation_action("approve", generation_id)

    def block(
        generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）"),
        reason_code: Literal["usage_limit_reached", "codex_imagegen_unavailable", "output_unavailable"] = typer.Option(
            ..., "--reason-code", help="生成開始前に停止した理由"
        ),
        message: str = typer.Option(..., "--message", help="利用者に伝える停止理由")
    ) -> None:
        _record_generation_action("block", generation_id, reason_code=reason_code, message=message)

    def mark_unknown(
        generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）"),
        reason: str = typer.Option(..., "--reason", help="結果を確定できない理由"),
    ) -> None:
        _record_generation_action("mark-unknown", generation_id, reason=reason)

    def fail(
        generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）"),
        reason_code: Literal["codex_imagegen_unavailable", "output_transfer_failed", "disk_full", "permission_denied"] = typer.Option(
            ..., "--reason-code", help="生成開始後に確認された失敗理由"
        ),
        message: str = typer.Option(..., "--message", help="失敗内容")
    ) -> None:
        _record_generation_action("fail", generation_id, reason_code=reason_code, message=message)

    def cancel(
        generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）"),
        reason: str = typer.Option(..., "--reason", help="取消理由"),
    ) -> None:
        _record_generation_action("cancel", generation_id, reason=reason)

    group.command("request-approval")(request_approval)
    group.command("approve")(approve)
    group.command("block")(block)
    group.command("mark-unknown")(mark_unknown)
    group.command("fail")(fail)
    group.command("cancel")(cancel)


_add_generation_lifecycle_commands(generate_app)
_add_generation_lifecycle_commands(edit_app)


@generate_app.command("list")
def generate_list_command(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    terminal: bool = typer.Option(False, "--terminal", help="エディタを開かず全文を端末へ表示"),
) -> None:
    """案件内の生成計画（新規生成・編集）を全状態、新しい順で一覧表示します。"""
    try:
        generations = list_generations(project_id)
    except ConfigError as exc:
        console.print(f"[red]設定エラー:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except OSError as exc:
        console.print(f"[red]生成計画一覧を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    _show_list(f"生成計画一覧 / 案件ID: {project_id}", [
        [
            ("生成計画ID（Generation ID）", generation.id),
            ("操作", generation.operation), ("状態", generation.status),
            ("作成日時(UTC)", generation.created_at.isoformat()),
            ("プロンプト", generation.original_instruction),
            ("指定参照", "\n".join(
                f"{ref.id} / {ref.role} / 優先度 {ref.priority} / {ref.description}"
                for ref in generation.reference_snapshots
            ) or "なし"),
            ("希望枚数（目安）", generation.requested_count_hint),
            ("登録候補数", generation.registered_candidate_count),
            ("編集元候補ID", generation.source_candidate_id or "なし"),
            ("Plan hash（生成計画IDとは別）", generation.plan_hash),
        ]
        for generation in generations
    ], terminal=terminal)


@generate_app.command("plan")
def generate_plan_command(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    prompt: str = typer.Option(..., "--prompt", help="今回の生成指示"),
    reference_ids: list[str] = typer.Option([], "--reference", help="使用する参照ID（複数指定可）"),
    count: int | None = typer.Option(None, "--count", min=1, max=10, help="希望枚数の目安（1〜10）"),
) -> None:
    """Codex会話用の固定スナップショットを保存します。画像生成は行いません。"""
    try:
        generation = plan_generate(
            project_id,
            prompt=prompt,
            reference_ids=reference_ids,
            count=count,
        )
    except GenerationPlanError as exc:
        console.print(f"[red]生成計画を作成できません:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]生成計画を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    _show_generation(generation)
    console.print("生成計画を保存しました。Codex会話で内容を確認してから画像を生成してください。")


@generate_app.command("show")
def generate_show_command(generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）")) -> None:
    """生成計画と実行結果を表示します。"""
    try:
        generation, _ = get_generation(generation_id)
    except ValueError as exc:
        console.print(f"[red]生成計画IDまたは内容が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]生成計画を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    _show_generation(generation)


@generate_app.command("register")
def generate_register_command(
    generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）"),
    image_paths: list[Path] = typer.Argument(..., help="Codex会話から保存した画像ファイル"),
) -> None:
    """画像を検証して生成候補として登録します。"""
    _register_generation_command(generation_id, image_paths)


@report_app.command("show")
def report_show_command(generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）")) -> None:
    """生成計画のMarkdownレポートを表示します。"""
    try:
        generation, root = get_generation(generation_id)
        report = read_report(root, str(generation.id))
    except ValueError as exc:
        console.print(f"[red]生成計画IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]レポートを読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(render_markdown(report), markup=False)


@report_app.command("open")
def report_open_command(generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）")) -> None:
    """生成計画のMarkdownレポートを既定の関連アプリで開きます。"""
    try:
        generation, root = get_generation(generation_id)
        path = root / "reports" / f"{generation.id}.md"
        if not path.is_file():
            raise FileNotFoundError("レポートファイルがありません。")
    except ValueError as exc:
        console.print(f"[red]生成計画IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except OSError as exc:
        console.print(f"[red]レポートを開けません:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    if os.name != "nt":
        console.print("[red]レポートを既定のアプリで開く操作はWindowsで利用できます。[/red]")
        raise typer.Exit(code=6)
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except OSError as exc:
        console.print(f"[red]既定のアプリで開けませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc


@edit_app.command("plan")
def edit_plan_command(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    source_candidate_id: str = typer.Option(..., "--source", help="編集元候補ID（UUID v4）"),
    prompt: str = typer.Option(..., "--prompt", help="今回の編集指示"),
    reference_ids: list[str] = typer.Option([], "--reference", help="送信する追加参照ID（複数指定可）"),
    count: int | None = typer.Option(None, "--count", min=1, max=10, help="希望枚数の目安（1〜10）"),
) -> None:
    """Codex会話用の候補画像を編集する生成計画を保存します。画像生成は行いません。"""
    try:
        generation = plan_edit(
            project_id,
            source_candidate_id=source_candidate_id,
            prompt=prompt,
            reference_ids=reference_ids,
            count=count,
        )
    except GenerationPlanError as exc:
        console.print(f"[red]編集用の生成計画を作成できません:[/red] {exc}")
        raise typer.Exit(code=3) from exc
    except ValueError as exc:
        console.print(f"[red]入力が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except LockConflict as exc:
        console.print(f"[red]案件が使用中です:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]編集用の生成計画を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    _show_generation(generation)
    console.print("編集用の生成計画を保存しました。Codex会話で内容を確認してから画像を編集してください。")


@edit_app.command("show")
def edit_show_command(generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）")) -> None:
    """編集用の生成計画と実行結果を表示します。"""
    generate_show_command(generation_id)


@edit_app.command("register")
def edit_register_command(
    generation_id: str = typer.Argument(..., help="生成計画ID（UUID v4）"),
    image_paths: list[Path] = typer.Argument(..., help="Codex会話から保存した編集画像ファイル"),
) -> None:
    """編集した画像を検証して候補として登録します。"""
    _register_generation_command(generation_id, image_paths)


@candidate_app.command("list")
def candidate_list_command(
    project_id: str = typer.Argument(..., help="案件ID（UUID v4）"),
    generation_id: str | None = typer.Option(None, "--generation", help="生成計画IDで絞り込み"),
    terminal: bool = typer.Option(False, "--terminal", help="エディタを開かず全文を端末へ表示"),
) -> None:
    """候補を案件または生成計画ごとに一覧表示します。"""
    try:
        candidates = list_candidates(project_id, generation_id=generation_id)
    except ValueError as exc:
        console.print(f"[red]IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]候補一覧を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    _show_list(f"候補一覧 / 案件ID: {project_id}", [
        [("候補ID", candidate.id), ("生成計画ID", candidate.generation_id), ("状態", candidate.status),
         ("寸法", f"{candidate.width}×{candidate.height}"),
         ("親候補", candidate.parent_candidate_id or "なし"), ("SHA-256", candidate.sha256)]
        for candidate in candidates
    ], terminal=terminal)


@candidate_app.command("show")
def candidate_show_command(candidate_id: str = typer.Argument(..., help="候補ID（UUID v4）")) -> None:
    """候補の詳細とレビューを表示します。"""
    try:
        candidate, project, root = find_candidate(candidate_id)
        image_path = safe_project_path(root, candidate.file)
    except ValueError as exc:
        console.print(f"[red]候補IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]候補を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    table = Table(title=f"候補: {candidate.id}")
    table.add_column("項目", style="cyan")
    table.add_column("値")
    table.add_row("案件", f"{project.name} ({project.id})")
    table.add_row("生成計画ID", str(candidate.generation_id))
    table.add_row("状態", candidate.status)
    table.add_row("画像", candidate.file)
    table.add_row("画像の存在", "あり" if image_path.is_file() else "なし")
    table.add_row("SHA-256", candidate.sha256)
    table.add_row("寸法", f"{candidate.width}×{candidate.height}")
    table.add_row("MIME type", candidate.mime_type)
    table.add_row("親候補", str(candidate.parent_candidate_id or "なし"))
    table.add_row("レビューID", ", ".join(str(item) for item in candidate.review_ids) or "なし")
    console.print(table)


@candidate_app.command("open")
def candidate_open_command(candidate_id: str = typer.Argument(..., help="候補ID（UUID v4）")) -> None:
    """候補画像をWindowsの既定アプリで開きます。"""
    try:
        candidate, _, root = find_candidate(candidate_id)
        image_path = safe_project_path(root, candidate.file)
        if not image_path.is_file() or sha256_file(image_path) != candidate.sha256:
            raise StorageError("候補画像が見つからないかSHA-256が一致しません。")
    except ValueError as exc:
        console.print(f"[red]候補IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]候補画像を開けません:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    if os.name != "nt":
        console.print("[red]候補ビューアはWindows環境で利用できます。[/red]")
        raise typer.Exit(code=6)
    try:
        subprocess.Popen(["explorer.exe", str(image_path)], shell=False)
    except OSError as exc:
        console.print(f"[red]既定アプリを起動できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc


@candidate_app.command("compare")
def candidate_compare_command(
    candidate_ids: list[str] = typer.Argument(..., help="比較する候補ID（2件以上）"),
) -> None:
    """候補情報を並べて表示し、各画像を既定アプリで開きます。"""
    if len(candidate_ids) < 2:
        console.print("[red]比較には候補IDを2件以上指定してください。[/red]")
        raise typer.Exit(code=2)
    candidates = []
    try:
        for candidate_id in candidate_ids:
            candidate, project, root = find_candidate(candidate_id)
            image_path = safe_project_path(root, candidate.file)
            if not image_path.is_file() or sha256_file(image_path) != candidate.sha256:
                raise StorageError(f"候補画像を検証できません: {candidate.id}")
            candidates.append((candidate, project, image_path))
    except ValueError as exc:
        console.print(f"[red]候補IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]比較画像を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    table = Table(title="候補比較")
    table.add_column("候補ID", style="cyan")
    table.add_column("案件")
    table.add_column("状態")
    table.add_column("寸法")
    table.add_column("親候補")
    table.add_column("ファイル")
    for candidate, project, path in candidates:
        table.add_row(
            str(candidate.id), project.name, candidate.status,
            f"{candidate.width}×{candidate.height}",
            str(candidate.parent_candidate_id or "なし"), str(path),
        )
    console.print(table)
    if os.name != "nt":
        console.print("[red]候補ビューアはWindows環境で利用できます。[/red]")
        raise typer.Exit(code=6)
    for _, _, image_path in candidates:
        subprocess.Popen(["explorer.exe", str(image_path)], shell=False)


def _prompt_choice(prompt: str, choices: set[str]) -> str:
    while True:
        value = typer.prompt(f"{prompt} ({'/'.join(sorted(choices))})").strip()
        if value in choices:
            return value
        console.print(f"[red]次の値から選んでください: {' / '.join(sorted(choices))}[/red]")


@app.command("review")
def review_command(candidate_id: str = typer.Argument(..., help="レビューする候補ID（UUID v4）")) -> None:
    """有効要件ごとの判定を質問し、新しいレビューとして保存します。"""
    try:
        candidate, project, _ = find_candidate(candidate_id)
        if project.status != "active":
            raise CandidateStateConflict("アーカイブ済み案件は変更できません。")
        requirements = list_requirements(project.id)
    except ValueError as exc:
        console.print(f"[red]候補IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except CandidateStateConflict as exc:
        console.print(f"[red]レビューできません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]レビュー情報を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    console.print(f"候補: {candidate.id} / 案件: {project.name}")
    results: list[RequirementResult] = []
    allowed = {"pass", "fail", "uncertain", "not_applicable"}
    for requirement in requirements:
        console.print(
            f"要件 {requirement.id} ({requirement.category}, 優先度 {requirement.priority}): "
            f"{requirement.description}"
        )
        result = _prompt_choice("判定", allowed)
        comment = typer.prompt("コメント", default="")
        results.append(
            RequirementResult(
                requirement_id=requirement.id,
                result=result,
                comment=comment,
            )
        )
    issues_text = typer.prompt("問題点（なければ空欄）", default="")
    rationale = typer.prompt("総評")
    issues = [line.strip() for line in issues_text.splitlines() if line.strip()]
    if not typer.confirm("このレビュー記録を保存しますか？", default=True):
        console.print("レビューを保存せず中止しました。")
        raise typer.Exit(code=130)
    try:
        review = review_candidate(
            candidate.id,
            requirement_results=results,
            issues=issues,
            rationale=rationale,
        )
    except CandidateStateConflict as exc:
        console.print(f"[red]レビューを保存できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except CandidateNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]レビューを保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"レビューを保存しました: {review.id}")
    _refresh_candidate_report(str(candidate.id))


def _candidate_status_command(
    candidate_id: str,
    *,
    status: Literal["selected", "rejected", "on_hold"],
    reason: str,
) -> None:
    try:
        candidate, project, _ = find_candidate(candidate_id)
    except ValueError as exc:
        console.print(f"[red]候補IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    console.print(f"対象候補: {candidate.id} / 案件: {project.name} / 現在状態: {candidate.status}")
    console.print(f"変更後状態: {status} / 理由: {reason}")
    if not typer.confirm("候補の状態を変更しますか？", default=False):
        console.print("状態変更を中止しました。")
        raise typer.Exit(code=130)
    try:
        updated = update_candidate_status(candidate.id, status=status, reason=reason)
    except CandidateStateConflict as exc:
        console.print(f"[red]候補状態を変更できません:[/red] {exc}")
        raise typer.Exit(code=4) from exc
    except ValueError as exc:
        console.print(f"[red]理由が不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]候補状態を保存できませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc
    console.print(f"候補の状態を更新しました: {updated.id} ({updated.status})")
    _refresh_candidate_report(str(updated.id))


@candidate_app.command("select")
def candidate_select_command(
    candidate_id: str = typer.Argument(..., help="候補ID（UUID v4）"),
    reason: str = typer.Option(..., "--reason", help="選択理由"),
) -> None:
    """候補をselectedにします。採用版設定とは別の操作です。"""
    _candidate_status_command(candidate_id, status="selected", reason=reason)


@candidate_app.command("reject")
def candidate_reject_command(
    candidate_id: str = typer.Argument(..., help="候補ID（UUID v4）"),
    reason: str = typer.Option(..., "--reason", help="却下理由"),
) -> None:
    """候補をrejectedにします。"""
    _candidate_status_command(candidate_id, status="rejected", reason=reason)


@candidate_app.command("hold")
def candidate_hold_command(
    candidate_id: str = typer.Argument(..., help="候補ID（UUID v4）"),
    reason: str = typer.Option(..., "--reason", help="保留理由"),
) -> None:
    """候補をon_holdにします。"""
    _candidate_status_command(candidate_id, status="on_hold", reason=reason)


@app.command("status")
def status_show(project_id: str | None = typer.Argument(None, help="案件ID（省略時は全案件）")) -> None:
    """案件の状態、生成計画、レビュー待ち、次の操作を表示します。"""
    try:
        if project_id is None:
            projects = list_projects()
            table = Table(title="案件の状態")
            table.add_column("案件ID", style="cyan")
            table.add_column("名前")
            table.add_column("状態")
            table.add_column("採用版候補ID")
            for project in projects:
                table.add_row(
                    str(project.id), project.name, project.status,
                    str(project.approved_candidate_id or "未設定"),
                )
            console.print(table)
            return

        project = get_project(project_id)
        root = project_root(project.id)
        generations = sorted(
            (
                read_model(path, Generation)
                for path in (root / "generations").glob("*/generation.json")
            ),
            key=lambda item: (item.created_at, str(item.id)),
        )
        candidates = [
            read_model(path, Candidate)
            for path in sorted((root / "candidates").glob("*.json"))
        ]
    except ValueError as exc:
        console.print(f"[red]案件IDが不正です:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=4) from exc
    except (OSError, StorageError) as exc:
        console.print(f"[red]案件の状態を読み込めませんでした:[/red] {exc}")
        raise typer.Exit(code=6) from exc

    table = Table(title=f"案件の状態: {project.name}")
    table.add_column("項目", style="cyan")
    table.add_column("内容")
    table.add_row("案件ID", str(project.id))
    table.add_row("状態", project.status)
    table.add_row("採用版候補ID", str(project.approved_candidate_id or "未設定"))
    active_generations = [
        item for item in generations
        if item.status in {"draft", "awaiting_approval", "running", "result_unknown"}
    ]
    table.add_row(
        "進行中・未確認の生成計画",
        ", ".join(f"{item.id} ({item.status})" for item in active_generations) or "なし",
    )
    pending_review = [
        item for item in candidates if item.status == "pending" and not item.review_ids
    ]
    table.add_row("レビュー待ち候補", str(len(pending_review)))

    if project.status == "archived":
        next_action = "アーカイブ済みのため読み取りのみ可能です。"
    elif active_generations:
        current = active_generations[-1]
        command = "generate" if current.operation == "generate" else "edit"
        next_action = {
            "draft": f"生成計画を提示し、承認待ちを記録してください: iwb {command} request-approval {current.id}",
            "awaiting_approval": f"明示承認後に承認を記録してください: iwb {command} approve {current.id}",
            "running": f"会話結果を確認し、画像を登録または状態を記録してください: iwb {command} show {current.id}",
            "result_unknown": f"確認するまで再生成しないでください: iwb {command} show {current.id}",
        }[current.status]
    elif generations and generations[-1].status == "blocked":
        next_action = "利用上限回復を確認し、利用者が新しい生成計画を依頼した場合にだけ再開してください。"
    elif pending_review:
        next_action = f"候補を比較してレビューしてください: iwb review {pending_review[0].id}"
    elif generations and generations[-1].status in {"failed", "partially_succeeded"}:
        next_action = f"失敗枠を確認してください: iwb generate show {generations[-1].id}"
    else:
        next_action = "要件を確認し、必要な参照画像を登録してからgenerate/edit planを作成してください。"
    table.add_row("次の操作", next_action)
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
