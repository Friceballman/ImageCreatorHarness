"""安全なローカルファイル保存、案件ロック、パス・ハッシュ検証。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from image_workbench.versions import UnsupportedSchemaVersion, require_supported_schema


ModelT = TypeVar("ModelT", bound=BaseModel)


class StorageError(OSError):
    """永続化に関する操作エラー。"""


class LockConflict(StorageError):
    """案件ロックを取得できなかった。"""


def create_staging_directory(parent: Path, *, prefix: str) -> Path:
    """確定前の作業領域を作る。Windowsでは親のアクセス権を継承する。"""
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))

    # mkdtempのmode=0o700はWindowsでACL継承を無効にする。
    # UUIDと排他的mkdirで衝突を避け、サンドボックス用の権限も継承する。
    for _ in range(128):
        directory = parent / f"{prefix}{uuid4().hex}"
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return directory
    raise FileExistsError("一時フォルダーの一意な名前を確保できませんでした。")


def safe_project_path(project_root: Path, relative_posix_path: str) -> Path:
    """案件ルート内に収まる既存または新規パスを解決する。"""
    if not relative_posix_path or "\\" in relative_posix_path:
        raise ValueError("保存パスは案件ルート相対のPOSIX形式で指定してください。")
    relative = PurePosixPath(relative_posix_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative_posix_path.split("/")):
        raise ValueError("案件ルート外を指す保存パスは使用できません。")

    root = project_root.resolve(strict=True)
    destination = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ValueError("案件ルート外を指す保存パスは使用できません。") from exc
    return destination


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """ファイルのSHA-256をストリーミング計算する。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_temp_bytes(directory: Path, content: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".iwb-", suffix=".tmp", dir=directory)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def atomic_write_bytes(path: Path, content: bytes, *, replace: bool = False) -> None:
    """同一ディレクトリの一時ファイルから原子的にバイト列を保存する。

    replace=Falseでは既存の最終名を再利用しない。
    """
    temp_path = _write_temp_bytes(path.parent, content)
    try:
        if replace:
            os.replace(temp_path, path)
        else:
            # 同一ボリューム上のハードリンク作成は、既存名を上書きせず原子的に確定する。
            os.link(temp_path, path)
            temp_path.unlink()
    except FileExistsError as exc:
        raise StorageError(f"保存先は既に存在します。既存ファイルは上書きしません: {path}") from exc
    except OSError as exc:
        raise StorageError(f"ファイルを確定できません: {path}: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_copy_file(source: Path, destination: Path) -> str:
    """ファイルを同一ディレクトリの一時名へコピーし、検証後に新規名で確定する。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".iwb-", suffix=".tmp", dir=destination.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as output_stream:
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        digest = sha256_file(temp_path)
        os.link(temp_path, destination)
        temp_path.unlink()
        return digest
    except FileExistsError as exc:
        raise StorageError(
            f"保存先は既に存在します。既存ファイルは上書きしません: {destination}"
        ) from exc
    except OSError as exc:
        raise StorageError(f"ファイルをコピーできません: {source} -> {destination}: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_model(path: Path, model: BaseModel, *, replace: bool = False) -> None:
    """Pydanticモデルを検証可能なUTF-8 JSONとして原子的に保存する。"""
    content = (model.model_dump_json(indent=2) + "\n").encode("utf-8")
    try:
        decoded = json.loads(content.decode("utf-8"))
        require_supported_schema(decoded)
        type(model).model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, UnsupportedSchemaVersion) as exc:
        raise StorageError(f"保存前のJSON再検証に失敗しました: {path}: {exc}") from exc
    atomic_write_bytes(path, content, replace=replace)


def read_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """JSONを読み、スキーマ版とPydanticモデルの両方で検証する。"""
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        require_supported_schema(data)
        return model_type.model_validate(data)
    except UnsupportedSchemaVersion as exc:
        raise StorageError(
            f"未対応スキーマのため読み込めません: {path}: {exc}"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise StorageError(
            f"JSONを読み込めないか内容が不正です: {path}: {exc}。"
            "元ファイルを保全し、バックアップからの復旧を確認してください。"
        ) from exc


class ProjectLock:
    """排他的な案件ロック。残存ロックは自動で解除しない。"""

    def __init__(self, project_root: Path) -> None:
        self.path = project_root / ".iwb.lock"
        self._token = f"pid={os.getpid()}\nstarted_at={datetime.now(timezone.utc).isoformat()}\n"
        self._owned = False

    def __enter__(self) -> "ProjectLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            try:
                lock_details = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                lock_details = "PIDと開始時刻を読み取れません"
            raise LockConflict(
                f"案件ロックを取得できません: {self.path} ({lock_details})。"
                "実行中の書き込みがないことを確認してから対処してください。"
            ) from exc

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(self._token)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self.path.unlink(missing_ok=True)
            raise
        self._owned = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._owned:
            return
        try:
            if self.path.read_text(encoding="utf-8") == self._token:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._owned = False
