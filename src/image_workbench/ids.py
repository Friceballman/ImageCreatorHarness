"""仕様で定めるUUID v4の検証と正規化。"""

from uuid import UUID


def parse_uuid4(value: str | UUID) -> UUID:
    """UUID v4を受け取り、UUIDオブジェクトへ正規化する。"""
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("IDはUUID v4で指定してください。") from exc

    if parsed.version != 4:
        raise ValueError("IDはUUID v4で指定してください。")
    return parsed


def normalize_uuid4(value: str | UUID) -> str:
    """UUID v4を小文字・ハイフン付きの標準表記で返す。"""
    return str(parse_uuid4(value))
