"""一覧スナップショットのキャッシュと、待機しないVS Code起動。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import UUID4

from image_workbench.ids import parse_uuid4
from image_workbench.models import Record, UTCDateTime
from image_workbench.storage import (
    StorageError, atomic_write_bytes, atomic_write_model, create_staging_directory, read_model,
)


class ListCache(Record):
    id: UUID4
    created_at: UTCDateTime
    title: str
    file: Literal["listing.txt"] = "listing.txt"


def cache_root() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ImageWorkbench" / "cache" / "lists"
    return Path.home() / ".cache" / "image-workbench" / "lists"


def _ordinary(path: Path) -> bool:
    return not path.is_symlink() and not path.is_junction()


def _validate_cache(root: Path, cache_id: str | UUID) -> tuple[ListCache, Path]:
    identifier = parse_uuid4(cache_id)
    if not _ordinary(root):
        raise StorageError("一覧キャッシュの保存先にリンクは使用できません。")
    directory = root / str(identifier)
    if not directory.is_dir() or not _ordinary(directory):
        raise StorageError(f"一覧キャッシュの保存先が不正です: {identifier}")
    if directory.resolve(strict=True).parent != root.resolve(strict=True):
        raise StorageError("一覧キャッシュの範囲外は削除できません。")
    entries = list(directory.iterdir())
    allowed = {"listing.txt", "manifest.json"}
    if any(p.name not in allowed or not p.is_file() or not _ordinary(p) for p in entries):
        raise StorageError(f"想定外のファイルを含むキャッシュは保持します: {identifier}")
    record = read_model(directory / "manifest.json", ListCache)
    if record.id != identifier:
        raise StorageError(f"キャッシュIDが記録と一致しません: {identifier}")
    return record, directory


def list_caches() -> tuple[list[ListCache], list[str]]:
    """削除候補を読み取る。作成中・未知・破損したものは保持する。"""
    root = cache_root()
    if not root.exists():
        return [], []
    if not _ordinary(root):
        raise StorageError("一覧キャッシュの保存先にリンクは使用できません。")
    records, warnings = [], []
    for directory in root.iterdir():
        try:
            parse_uuid4(directory.name)
        except ValueError:
            continue
        try:
            record, _ = _validate_cache(root, directory.name)
            records.append(record)
        except (OSError, ValueError):
            warnings.append(f"検証できない一覧キャッシュを保持します: {directory}")
    return sorted(records, key=lambda record: (record.created_at, str(record.id))), warnings


def clear_caches(cache_ids: list[UUID]) -> tuple[int, list[str]]:
    """明示確認されたIDだけを再検証して削除する。再帰削除しない。"""
    root = cache_root()
    removed, errors = 0, []
    for identifier in cache_ids:
        try:
            _, directory = _validate_cache(root, identifier)
            deleting = root / f".deleting-{uuid4()}"
            os.rename(directory, deleting)
            try:
                entries = list(deleting.iterdir())
                if any(p.name not in {"listing.txt", "manifest.json"} or
                       not p.is_file() or not _ordinary(p) for p in entries):
                    raise StorageError("削除確認後にキャッシュの内容が変更されました。")
                (deleting / "listing.txt").unlink(missing_ok=True)
                (deleting / "manifest.json").unlink()
                deleting.rmdir()
                removed += 1
            except OSError:
                if not directory.exists():
                    os.rename(deleting, directory)
                raise
        except (OSError, ValueError):
            errors.append(f"一覧キャッシュを削除できませんでした: {identifier}。保存先を確認して再実行してください。")
    return removed, errors


def render_listing(title: str, records: list[list[tuple[str, object]]]) -> str:
    blocks = [title, f"件数: {len(records)}"]
    for index, fields in enumerate(records, 1):
        lines = [f"--- {index} ---"]
        for label, value in fields:
            text = str(value).replace("\r\n", "\n").replace("\r", "\n")
            lines.append(f"{label}: " + text.replace("\n", "\n  "))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _write_cache(title: str, text: str) -> Path:
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    if not _ordinary(root):
        raise StorageError("一覧キャッシュの保存先にリンクは使用できません。")
    record = ListCache(id=uuid4(), created_at=datetime.now(timezone.utc), title=title)
    content = f"取得日時(UTC): {record.created_at.isoformat()}\n\n{text}".encode("utf-8")
    directory = root / str(record.id)
    temporary = create_staging_directory(root, prefix=".creating-")
    try:
        path = temporary / "listing.txt"
        atomic_write_bytes(path, content)
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(content).digest():
            raise StorageError("一覧キャッシュの保存後hashが一致しません。")
        atomic_write_model(temporary / "manifest.json", record)
        if directory.exists():
            raise StorageError("一覧キャッシュの保存先が既に存在します。上書きしません。")
        os.rename(temporary, directory)
        return directory / "listing.txt"
    finally:
        if temporary.exists():
            for filename in ("listing.txt", "manifest.json"):
                (temporary / filename).unlink(missing_ok=True)
            temporary.rmdir()


def _editor_command(path: Path) -> tuple[list[str], bool]:
    launcher = shutil.which("code")
    if not launcher:
        raise OSError("VS Codeのcodeコマンドが見つかりません。")
    options = ["--skip-add-to-recently-opened", str(path)]
    if os.name != "nt":
        return [launcher, *options], False
    script = Path(launcher)
    if script.suffix.lower() != ".cmd":
        raise OSError("対応するVS Codeのcode.cmdを確認できません。")
    try:
        content = script.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise OSError("VS Codeの起動構成を読み取れません。") from exc
    match = re.search(r'"%~dp0([^"\r\n]+\.exe)"\s+"%~dp0([^"\r\n]+cli\.js)"', content, re.I)
    if match is None:
        raise OSError("VS Codeの起動構成を確認できません。")
    install_root = script.parent.parent.resolve(strict=True)
    paths = [(script.parent / part).resolve(strict=True) for part in match.groups()]
    if any(not p.is_relative_to(install_root) or not p.is_file() for p in paths):
        raise OSError("VS Codeの起動パスがインストール先の範囲外です。")
    return [str(p) for p in paths] + options, True


def _start_editor(command: list[str], electron_cli: bool) -> subprocess.Popen:
    keys = ("ELECTRON_RUN_AS_NODE", "VSCODE_DEV") if electron_cli else ()
    previous = {key: os.environ.get(key) for key in keys}
    try:
        if electron_cli:
            os.environ["ELECTRON_RUN_AS_NODE"] = "1"
            os.environ.pop("VSCODE_DEV", None)
        return subprocess.Popen(
            command, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def show_listing(title: str, records: list[list[tuple[str, object]]], *, terminal: bool = False) -> None:
    text = render_listing(title, records)
    if terminal or not records or not is_interactive():
        sys.stdout.write(text)
        return
    path = _write_cache(title, text)
    print(f"一覧キャッシュ: {path}")
    try:
        command, electron_cli = _editor_command(path)
        _start_editor(command, electron_cli)
    except OSError:
        print("エディタを起動できないため、一覧を端末に表示します。キャッシュは保持します。", file=sys.stderr)
        sys.stdout.write(text)
    else:
        print("VS Codeへ一覧の表示を依頼しました。このまま次の操作を行えます。")
