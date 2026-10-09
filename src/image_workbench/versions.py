"""永続化データのスキーマ互換性。"""

from __future__ import annotations

from typing import Any


CURRENT_SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(ValueError):
    """アプリケーションが扱えないJSONスキーマ。"""


def require_supported_schema(data: Any) -> None:
    """JSONのschema_versionを検証し、未知の版を明示的に拒否する。"""
    if not isinstance(data, dict):
        raise ValueError("JSONの最上位要素はオブジェクトである必要があります。")
    version = data.get("schema_version")
    if type(version) is not int or version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"未対応のschema_versionです: {version!r}。"
            f"このバージョンは{CURRENT_SCHEMA_VERSION}のみ読み取れます。"
        )
