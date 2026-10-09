"""TOML設定の読込と、設定値の出所を保持する。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(ValueError):
    """設定ファイルの読込または検証エラー。"""


class ConfigValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_model: Literal["gpt-image-2"] = "gpt-image-2"
    default_size: Literal["1024x1024", "1536x1024", "1024x1536", "auto"] = "1024x1024"
    default_quality: Literal["low", "medium", "high"] = "medium"
    default_output_format: Literal["png", "jpeg", "webp"] = "png"
    default_candidate_count: int = Field(default=4, ge=1, le=10)
    max_candidate_count: Literal[10] = 10
    projects_dir: Path = Path("projects")


@dataclass(frozen=True)
class ResolvedConfig:
    values: ConfigValues
    sources: dict[str, Literal["command_line", "config.toml", "built_in"]]
    api_key_configured: bool


def _format_validation_error(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        parts.append(f"{location}: {item['msg']}")
    return "\n".join(parts)


def load_config(
    config_path: Path | None = None,
    *,
    cli_overrides: Mapping[str, Any] | None = None,
) -> ResolvedConfig:
    """CLI引数、config.toml、組み込み値の順で設定を解決する。

    APIキーの値は読み込まず、OPENAI_API_KEYが設定されているかだけを返す。
    """
    path = config_path or (Path.cwd() / "config.toml")
    values: dict[str, Any] = {}
    sources: dict[str, Literal["command_line", "config.toml", "built_in"]] = {
        "default_model": "built_in",
        "default_size": "built_in",
        "default_quality": "built_in",
        "default_output_format": "built_in",
        "default_candidate_count": "built_in",
        "max_candidate_count": "built_in",
        "projects_dir": "built_in",
    }

    if path.exists():
        try:
            with path.open("rb") as stream:
                file_values = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"設定ファイルを読み込めません: {path}: {exc}") from exc
        try:
            checked_file_values = ConfigValues.model_validate(file_values)
        except ValidationError as exc:
            raise ConfigError(
                f"設定ファイルの値が不正です: {path}\n{_format_validation_error(exc)}"
            ) from exc
        values.update(checked_file_values.model_dump(exclude_unset=True))
        for key in values:
            sources[key] = "config.toml"
    elif config_path is not None:
        raise ConfigError(f"指定された設定ファイルがありません: {path}")

    if cli_overrides:
        values.update(cli_overrides)
        for key in cli_overrides:
            if key not in sources:
                raise ConfigError(f"未対応の設定項目です: {key}")
            sources[key] = "command_line"

    try:
        checked_values = ConfigValues.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(f"設定値が不正です。\n{_format_validation_error(exc)}") from exc

    return ResolvedConfig(
        values=checked_values,
        sources=sources,
        api_key_configured=bool(os.environ.get("OPENAI_API_KEY")),
    )
