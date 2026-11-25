"""多语杯测短评解析。

解析目标（每条意见都可回溯）：
- 被否定的属性：如 “no ferment defect”“没有酒香”，polarity=NEGATED；
- 与对照杯的比较：如 “the reference Y04 …”“比 F23 …”，记 compared_cup；
- 引用主审用词：如 “像主审曾说的‘发酵’”，kind=HEAD_QUOTE 且不冒充描述人自己的判断；
- 每条 Assertion 带句子序号、句内字符跨度、原句与杯位；
- 命中统一大类而非具体义项的词（如裸“发酵”）不强行归类，保留原语言片段转疑点。
"""
from __future__ import annotations

import re
from typing import Optional

from review_flow.models import (
    Assertion,
    AssertionKind,
    ParsedNote,
    Polarity,
    SenseType,
)
from review_flow.terminology import Glossary

# 句末标点：英文分号/句号，中文分号/句号/感叹号
_SENTENCE_SPLIT = re.compile(r"[;。；！!?？]")
# 子句切分（仅用于在句内定位多个意见，跨度仍以整句坐标系记录）
_CLAUSE_SPLIT = re.compile(r"[,，]")

_NEGATION_EN = re.compile(r"\b(no|without|not|none of)\b", re.IGNORECASE)
_NEGATION_ZH = re.compile(r"(没有|无|未确认|不是|并未|不含)")
_COMPARISON_EN = re.compile(r"\b(?:the\s+)?reference\s+([A-Za-z]\d+)\b", re.IGNORECASE)
_COMPARISON_ZH = re.compile(r"比\s*([A-Za-z]\d+)")
_HEAD_QUOTE = re.compile(r"(?:像|如同)?主审(?:曾)?说的?[“\"']([^”\"']+)[”\"']")
_HEAD_QUOTE_EN = re.compile(r"\bhead judge(?: once)? said\s*[“\"']([^”\"']+)[”\"']",
                            re.IGNORECASE)
_POUR_EN = re.compile(r"\b(?:rechecked|second) (?:at )?(?:second|third)? ?pour\b", re.IGNORECASE)
_POUR_ZH = re.compile(r"第(一|二|三|1|2|3)次冲注")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}
_CUP_TOKEN = re.compile(r"\b([A-Za-z]\d+)\b")
# 未登记词面兜底：英文形容词性短语（含名词修饰 finish/aroma 等），
# 已知词面与虚词在调用处过滤；中文片段由调用方按对照表过滤。
_FALLBACK_EN_PHRASE = re.compile(
    r"\b([a-z][a-z]+(?:\s+(?:finish|aroma|acidity|body|aftertaste|flavor|flavour))?)\b"
)
_EN_STOPWORDS = {
    "the", "and", "was", "were", "in", "at", "no", "not", "without", "for",
    "second", "pour", "rechecked", "reference", "clean", "dull", "like",
    "once", "said", "head", "judge", "with", "of", "none", "that", "this",
    "than", "more", "less", "first", "third",
    # 通用感官载体词，不构成可区分的词义证据
    "aroma", "the aroma", "finish", "acidity", "body", "aftertaste",
    "flavor", "flavour", "smell", "taste", "cup",
}


def split_sentences(text: str) -> list[tuple[int, str]]:
    """切成句子，返回 (相对全文的起始偏移, 句子文本)。"""
    sentences: list[tuple[int, str]] = []
    start = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        end = match.end()
        piece = text[start:end].strip()
        if piece:
            offset = text.index(piece, start)
            sentences.append((offset, piece))
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append((text.index(tail, start), tail))
    return sentences


class NoteParser:
    def __init__(self, glossary: Glossary) -> None:
        self.glossary = glossary

    def parse(self, record_id: str, judge: str, blind_cup: str,
              raw_text: str, pour_seq: Optional[int] = None) -> ParsedNote:
        note = ParsedNote(record_id=record_id, judge=judge,
                          blind_cup=blind_cup, raw_text=raw_text,
                          pour_seq=pour_seq)
        for sentence_index, (_, sentence) in enumerate(split_sentences(raw_text)):
            note.assertions.extend(
                self._parse_sentence(record_id, judge, blind_cup,
                                     sentence_index, sentence, note)
            )
        return note

    # ------------------------------------------------------------------
    def _parse_sentence(self, record_id: str, judge: str, blind_cup: str,
                        sentence_index: int, sentence: str,
                        note: ParsedNote) -> list[Assertion]:
        assertions: list[Assertion] = []

        # 1) 引用主审用词：整句单独成证，不与描述人自己的意见混同
        for match in _HEAD_QUOTE.finditer(sentence):
            term = match.group(1).strip()
            assertions.append(self._build(
                record_id, judge, blind_cup, sentence_index, sentence,
                match.span(), term, AssertionKind.HEAD_QUOTE,
                polarity=Polarity.AFFIRMED, quote_attribution="主审",
                uncertainty=bool(_NEGATION_ZH.search(sentence)),
                pour_seq=note.pour_seq,
            ))

        for match in _HEAD_QUOTE_EN.finditer(sentence):
            term = match.group(1).strip()
            assertions.append(self._build(
                record_id, judge, blind_cup, sentence_index, sentence,
                match.span(), term, AssertionKind.HEAD_QUOTE,
                polarity=Polarity.AFFIRMED, quote_attribution="head judge",
                uncertainty=False, pour_seq=note.pour_seq,
            ))

        # 2) 与对照杯比较
        compared = self._comparison_target(sentence)

        # 3) 句中词面逐义解析（按子句判断否定范围）
        for clause_start, clause in self._clauses(sentence):
            negated = bool(_NEGATION_EN.search(clause) or _NEGATION_ZH.search(clause))
            uncertainty = "未确认" in clause or "not confirmed" in clause.lower()
            seen_spans: list[tuple[int, int]] = []
            for result, term, local_span in self._iter_terms(clause):
                abs_span = (clause_start + local_span[0], clause_start + local_span[1])
                if any(not (abs_span[1] <= s0 or abs_span[0] >= s1) for s0, s1 in seen_spans):
                    continue
                seen_spans.append(abs_span)
                # 引用句里的词已单独登记，词面与引用跨度重叠时跳过，避免重复
                if any(q.kind == AssertionKind.HEAD_QUOTE
                       and not (abs_span[1] <= q.span[0] or abs_span[0] >= q.span[1])
                       for q in assertions):
                    continue
                kind = AssertionKind.COMPARISON if compared else AssertionKind.ATTRIBUTE
                assertions.append(self._build(
                    record_id, judge, blind_cup, sentence_index, sentence,
                    abs_span, term, kind,
                    polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
                    compared_cup=compared, uncertainty=uncertainty,
                    pour_seq=note.pour_seq,
                    sense_code=result.sense_code, sense_type=result.sense_type,
                    umbrella_codes=result.umbrella_codes,
                ))

        # 4) 比较句若没有任何已登记词面，也要留一条定位记录
        if compared and not any(a.kind == AssertionKind.COMPARISON for a in assertions):
            assertions.append(Assertion(
                record_id=record_id, judge=judge, blind_cup=blind_cup,
                sentence_index=sentence_index, span=(0, len(sentence)),
                sentence=sentence, kind=AssertionKind.COMPARISON,
                polarity=Polarity.AFFIRMED, term_text="",
                sense_type=SenseType.UNRESOLVED, compared_cup=compared,
                pour_seq=note.pour_seq,
            ))

        # 5) 兜底扫描：对照表覆盖不到的词面单独留证，转疑点而不是静默丢弃
        known_spans = [a.span for a in assertions]
        for match in _FALLBACK_EN_PHRASE.finditer(sentence):
            phrase = match.group(1).strip()
            if phrase.lower() in _EN_STOPWORDS:
                continue
            if self.glossary.resolve(phrase).sense_code is not None:
                continue
            span = match.span()
            if any(not (span[1] <= s0 or span[0] >= s1) for s0, s1 in known_spans):
                continue
            known_spans.append(span)
            assertions.append(Assertion(
                record_id=record_id, judge=judge, blind_cup=blind_cup,
                sentence_index=sentence_index, span=span, sentence=sentence,
                kind=AssertionKind.ATTRIBUTE,
                polarity=Polarity.NEGATED if negated_in(sentence) else Polarity.AFFIRMED,
                term_text=phrase, sense_type=SenseType.UNRESOLVED,
                pour_seq=note.pour_seq,
            ))
        return assertions

    def _build(self, record_id: str, judge: str, blind_cup: str,
               sentence_index: int, sentence: str, span: tuple[int, int],
               term: str, kind: AssertionKind, polarity: Polarity,
               quote_attribution: Optional[str] = None,
               compared_cup: Optional[str] = None,
               uncertainty: bool = False, pour_seq: Optional[int] = None,
               sense_code: Optional[str] = None,
               sense_type: SenseType = SenseType.UNRESOLVED,
               umbrella_codes: tuple[str, ...] = ()) -> Assertion:
        result = self.glossary.resolve(term)
        return Assertion(
            record_id=record_id, judge=judge, blind_cup=blind_cup,
            sentence_index=sentence_index, span=span, sentence=sentence,
            kind=kind, polarity=polarity, term_text=term,
            sense_code=sense_code or result.sense_code,
            sense_type=sense_type if sense_code else result.sense_type,
            umbrella_codes=umbrella_codes or result.umbrella_codes,
            compared_cup=compared_cup, quote_attribution=quote_attribution,
            uncertainty=uncertainty, pour_seq=pour_seq,
        )

    def _comparison_target(self, clause: str) -> Optional[str]:
        match = _COMPARISON_EN.search(clause) or _COMPARISON_ZH.search(clause)
        return match.group(1) if match else None

    def _clauses(self, sentence: str) -> list[tuple[int, str]]:
        parts: list[tuple[int, str]] = []
        last = 0
        for split in _CLAUSE_SPLIT.finditer(sentence):
            parts.append((last, sentence[last:split.start()]))
            last = split.end()
        parts.append((last, sentence[last:]))
        return [(start, text) for start, text in parts if text.strip()]

    def _iter_terms(self, clause: str):
        """在子句中找出对照表词面，按长度优先，避免短词覆盖长词。"""
        found = []
        lowered = clause.lower()
        for sense in self.glossary.senses():
            for form in sense.surface_forms():
                needle = form.strip().lower()
                if not needle:
                    continue
                start = lowered.find(needle)
                while start != -1:
                    found.append((start, start + len(needle), needle))
                    start = lowered.find(needle, start + 1)
        # 去重并按长度优先、位置排序
        found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        picked: list[tuple[int, int, str]] = []
        for start, end, term in found:
            if any(not (end <= s0 or start >= s1) for s0, s1, _ in picked):
                continue
            picked.append((start, end, term))
        picked.sort()
        for start, end, term in picked:
            yield self.glossary.resolve(term), term, (start, end)


def detect_pour_seq(text: str) -> Optional[int]:
    """从短评原文识别第几次冲注；不同冲注不是同一次判断。"""
    match = _POUR_ZH.search(text)
    if match:
        return _CN_NUM[match.group(1)]
    if _POUR_EN.search(text):
        return 2
    return None


def find_cup_token(text: str) -> Optional[str]:
    match = _CUP_TOKEN.search(text)
    return match.group(1) if match else None


def negated_in(text: str) -> bool:
    return bool(_NEGATION_EN.search(text) or _NEGATION_ZH.search(text))
