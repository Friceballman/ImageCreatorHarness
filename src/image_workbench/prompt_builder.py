"""仕様書の順序で最終プロンプトを組み立てる。"""

from __future__ import annotations

import re

from image_workbench.models import ReferenceSnapshot, RequirementSnapshot


class PromptError(ValueError):
    """実行可能なプロンプトを作成できない。"""


def build_final_prompt(
    *,
    project_goal: str,
    original_instruction: str,
    references: list[ReferenceSnapshot],
    requirements: list[RequirementSnapshot],
    requested_count_hint: int,
) -> str:
    """Codex会話へ渡す指示を、目的・参照・要件・希望枚数の順に構成する。"""
    if not project_goal.strip() or not original_instruction.strip():
        raise PromptError("案件の目的と今回の指示を指定してください。")

    sections = [
        ("目的", f"案件の目的: {project_goal.strip()}\n今回の指示: {original_instruction.strip()}"),
    ]
    if references:
        lines = [
            f"- ID: {item.id} / 役割: {item.role} / 説明: {item.description} / 優先度: {item.priority}"
            for item in references
        ]
        sections.append(("参照画像", "\n".join(lines)))

    category_sections = (
        ("must_keep", "維持条件"),
        ("must_change", "変更条件"),
        ("improve", "改善条件"),
    )
    for category, title in category_sections:
        entries = [item for item in requirements if item.category == category]
        if entries:
            sections.append(
                (title, "\n".join(f"- {item.description}" for item in entries))
            )

    sections.append(("出力構図", "利用者が指定した構図・形式の希望を優先してください。"))
    sections.append(
        ("希望枚数", f"希望枚数は{requested_count_hint}枚です。目安として扱い、厳密な一致は求めません。")
    )
    result = "\n\n".join(f"## {title}\n{body}" for title, body in sections)
    return result


def find_direct_requirement_conflicts(
    requirements: list[RequirementSnapshot],
) -> list[tuple[RequirementSnapshot, RequirementSnapshot]]:
    """維持と変更に同じ文言がある直接矛盾を返す。意味推測はしない。"""
    normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    keep = [item for item in requirements if item.category == "must_keep"]
    change = [item for item in requirements if item.category == "must_change"]
    return [
        (left, right)
        for left in keep
        for right in change
        if normalize(left.description) == normalize(right.description)
    ]
