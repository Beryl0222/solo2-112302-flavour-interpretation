"""多语词义对照表。

关键约束：
1. 上下位：具体义项挂在统一大类下；大类只方便检索，不能用大类把
   双方在不同义项上的意见直接抵消。
2. 条件适用词：如 winey 仅在洁净杯语境成立，语境不满足时不能直接采信。
3. 重新指定词义会产生新版本，复评目的可能因此从校准香型变为确认缺陷。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from review_flow.models import SenseType, TermSense


@dataclass(frozen=True)
class ResolveResult:
    """词面解析结果。"""

    term_text: str
    sense_code: Optional[str]
    sense_type: SenseType
    umbrella_codes: tuple[str, ...]
    sense: Optional[TermSense]


class Glossary:
    """词义对照表。查询走词面索引，构建由语料适配层完成。"""

    def __init__(self, revision: str = "v1") -> None:
        self.revision = revision
        self._senses: dict[str, TermSense] = {}
        self._form_index: dict[str, str] = {}
        self._history: list[tuple[str, str, str]] = []  # (revision, 词面, 新义项)

    # ---- 构建 ----------------------------------------------------------
    def add_sense(self, sense: TermSense) -> None:
        self._senses[sense.code] = sense
        for form in sense.surface_forms():
            self._form_index[form.strip().lower()] = sense.code

    # ---- 查询 ----------------------------------------------------------
    def get(self, code: str) -> TermSense:
        return self._senses[code]

    def has(self, code: str) -> bool:
        return code in self._senses

    def senses(self) -> tuple[TermSense, ...]:
        """全部已登记义项与大类。"""
        return tuple(self._senses.values())

    def is_umbrella(self, code: str) -> bool:
        sense = self._senses.get(code)
        return sense is not None and sense.sense_type == SenseType.UMBRELLA

    def resolve(self, term_text: str) -> ResolveResult:
        """把原语言词面解析到义项；未登记词返回 UNRESOLVED 疑点。"""
        key = term_text.strip().lower()
        code = self._form_index.get(key)
        if code is None:
            # 退一步：词面包含某个已登记形式（处理句中带标点的片段）
            for form, candidate in self._form_index.items():
                if form and form in key:
                    code = candidate
                    break
        if code is None:
            return ResolveResult(term_text, None, SenseType.UNRESOLVED, (), None)
        sense = self._senses[code]
        return ResolveResult(
            term_text, sense.code, sense.sense_type,
            sense.umbrella_codes, sense,
        )

    def senses_under_umbrella(self, umbrella_code: str) -> tuple[TermSense, ...]:
        return tuple(
            sense for sense in self._senses.values()
            if sense.sense_type == SenseType.SENSE and umbrella_code in sense.umbrella_codes
        )

    def share_umbrella(self, code_a: str, code_b: str) -> bool:
        """两个义项是否同属一个统一大类（同大类不等于意见可抵消）。"""
        if code_a not in self._senses or code_b not in self._senses:
            return False
        return bool(set(self._senses[code_a].umbrella_codes)
                    & set(self._senses[code_b].umbrella_codes))

    def conditional_context(self, code: str) -> Optional[str]:
        sense = self._senses.get(code)
        return sense.conditional if sense else None

    # ---- 重新指定词义 --------------------------------------------------
    def redesignate(self, term_text: str, target_sense_code: str,
                    new_revision: str) -> "Glossary":
        """把某词面重新指定到另一个义项，返回带新版本号的对照表。

        原表不修改；历史记录保留，供复评说明追溯“目的为何改变”。
        """
        if target_sense_code not in self._senses:
            raise KeyError(f"目标义项未登记: {target_sense_code}")
        target = self._senses[target_sense_code]
        if target.sense_type != SenseType.SENSE:
            raise ValueError("只能重新指定到具体义项，不能指定到统一大类")

        updated = Glossary(revision=new_revision)
        form_key = term_text.strip().lower()
        for sense in self._senses.values():
            languages = {
                lang: tuple(
                    f for f in forms if f.strip().lower() != form_key
                )
                for lang, forms in sense.languages.items()
            }
            languages = {lang: forms for lang, forms in languages.items() if forms}
            updated.add_sense(TermSense(
                code=sense.code, canonical=sense.canonical,
                sense_type=sense.sense_type, umbrella_codes=sense.umbrella_codes,
                languages=languages, conditional=sense.conditional,
                glossary_record_id=sense.glossary_record_id,
                revision=new_revision,
            ))
        # 把词面挂到目标义项（沿用原语言，未知语言归到 zxx 原片段）
        old = self.resolve(term_text)
        lang = "zxx"
        if old.sense is not None:
            for lang_code, forms in old.sense.languages.items():
                if any(f.strip().lower() == form_key for f in forms):
                    lang = lang_code
                    break
        re_target = updated._senses[target_sense_code]
        new_languages = dict(re_target.languages)
        new_languages[lang] = tuple([*new_languages.get(lang, ()), term_text.strip()])
        updated._senses[target_sense_code] = TermSense(
            code=re_target.code, canonical=re_target.canonical,
            sense_type=re_target.sense_type, umbrella_codes=re_target.umbrella_codes,
            languages=new_languages, conditional=re_target.conditional,
            glossary_record_id=re_target.glossary_record_id, revision=new_revision,
        )
        updated._form_index[form_key] = target_sense_code
        updated._history = [*self._history, (new_revision, term_text.strip(), target_sense_code)]
        return updated

    @property
    def history(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(self._history)
