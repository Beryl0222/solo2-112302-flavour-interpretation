"""盲审复评单、封存密钥与送达冻结。

信息隔离要求：
- 盲审组拿到的复评单只含盲号、角色和脱敏原句，看不到批号、产区、评委姓名，
  也无法通过复评单猜到同案其他杯的身份（不可关联审计）。
- 原产区负责人凭封存密钥从盲号收回结论与来源证据；密钥按产区隔离。
- 送达即冻结盲号对应；之后的复核说明不得暴露任何新的盲号对应关系。
"""
from __future__ import annotations

import random
import re

from review_flow.models import (
    Assertion,
    DeliveryRecord,
    Dispute,
    ReviewAssignment,
    ReviewSheet,
    SealedKey,
    SealedKeyEntry,
    SheetEntry,
)
from review_flow.provenance import ProvenanceRegistry

# 脱敏只做字面替换：批号、具体产区名、评委姓名由调用方按本场登记值注入，
# 不用宽泛正则，避免把“跨产区”这类通用词误删。
_PROMO_HINT = re.compile(r"宣传稿")


def redact(text: str, judge_names: tuple[str, ...] = (),
           regions: tuple[str, ...] = (),
           lot_codes: tuple[str, ...] = ()) -> str:
    """脱敏一段原文：保留杯号与感官词面，去掉身份与来源线索。"""
    out = _PROMO_HINT.sub("〔材料〕", text)
    for name in judge_names:
        out = out.replace(name, "〔评委〕")
    for region in sorted(regions, key=len, reverse=True):
        out = out.replace(region, "〔产区〕")
    for lot_code in sorted(lot_codes, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(lot_code)}\b", "〔批号〕", out)
    return out


class SheetBuilder:
    def __init__(self, provenance: ProvenanceRegistry,
                 judge_names: tuple[str, ...] = (),
                 seed: str = "cupping-review") -> None:
        self.provenance = provenance
        self.judge_names = judge_names
        self.regions = tuple(dict.fromkeys(lot.region for lot in provenance.lots))
        self.lot_codes = tuple(lot.lot_code for lot in provenance.lots)
        self._rng = random.Random(seed)

    def build(self, assignment: ReviewAssignment, dispute: Dispute) -> tuple[ReviewSheet, SealedKey]:
        if assignment.judge is None:
            raise ValueError("退回不足的派单不能生成盲审单")

        entries: list[SheetEntry] = []
        key_entries: list[SealedKeyEntry] = []

        # 待复评杯
        for cup in dispute.blind_cups:
            excerpt = self._excerpt(dispute.evidence_assertions, cup)
            entries.append(SheetEntry(
                entry_no=0, blind_cup=cup, role="待复评",
                redacted_excerpt=excerpt,
                doubts=tuple(d.doubt_id for d in dispute.doubts
                             if cup in d.blind_cups),
            ))
            key_entries.append(self._key_entry(cup))

        # 对照杯（只写角色，不写来源产区）
        for lot in assignment.controls:
            for cup in lot.blind_cups:
                entries.append(SheetEntry(
                    entry_no=0, blind_cup=cup, role="对照杯",
                    redacted_excerpt="按复评目的选配的跨产区适格对照，材料来源封存",
                ))
                key_entries.append(self._key_entry(cup))

        # 盲审单行序洗牌：防止行序暗示同案杯关系
        self._rng.shuffle(entries)
        for index, entry in enumerate(entries, start=1):
            entries[index - 1] = SheetEntry(
                entry_no=index, blind_cup=entry.blind_cup, role=entry.role,
                redacted_excerpt=entry.redacted_excerpt, doubts=entry.doubts,
            )

        open_doubts = tuple(
            f"{d.doubt_id}: {redact(d.reason, self.judge_names, self.regions, self.lot_codes)}"
            for d in assignment.doubts
        )
        sheet = ReviewSheet(
            sheet_id=f"S-{dispute.dispute_id}-{assignment.judge.name}",
            assignment_id=assignment.dispute_id,
            purpose=assignment.purpose, judge_name=assignment.judge.name,
            entries=entries, open_doubts=open_doubts,
        )
        region = self.provenance.require_lot_of_cup(dispute.blind_cups[0]).region
        sealed = SealedKey(
            key_id=f"K-{dispute.dispute_id}-{region}",
            sheet_id=sheet.sheet_id, region=region, entries=key_entries,
        )
        return sheet, sealed

    # ------------------------------------------------------------------
    def _excerpt(self, assertions: list[Assertion], cup: str) -> str:
        lines = []
        seen: set[tuple[int, int, str]] = set()
        for assertion in assertions:
            if assertion.blind_cup != cup:
                continue
            signature = (assertion.sentence_index, assertion.span[0], assertion.term_text)
            if signature in seen:
                continue
            seen.add(signature)
            fragment = assertion.sentence
            lines.append(redact(
                fragment, self.judge_names, self.regions, self.lot_codes,
            ))
        if not lines:
            return "（无登记短评，按盲号送复评，见封存疑点）"
        return " / ".join(dict.fromkeys(lines))

    def _key_entry(self, cup: str) -> SealedKeyEntry:
        lot = self.provenance.require_lot_of_cup(cup)
        record_ids = tuple(
            [*(e.record_id for e in lot.packaging_events),
             *( [lot.record_id] if lot.record_id else [])]
        )
        event_ids = tuple(e.event_id for e in lot.packaging_events)
        return SealedKeyEntry(
            blind_cup=cup, lot_code=lot.lot_code, region=lot.region,
            evidence_record_ids=tuple(dict.fromkeys(record_ids)),
            packaging_event_ids=event_ids,
        )


def deliver(sheet: ReviewSheet) -> DeliveryRecord:
    """送达并冻结：记录冻结杯位，之后说明不得再暴露新对应关系。"""
    sheet.delivered = True
    sheet.frozen_blind_cups = tuple(entry.blind_cup for entry in sheet.entries)
    return DeliveryRecord(
        sheet_id=sheet.sheet_id, judge_name=sheet.judge_name,
        purpose=sheet.purpose, frozen_cups=sheet.frozen_blind_cups,
    )


def assert_no_identity_leak(sheet: ReviewSheet, sealed: SealedKey,
                            lot_codes: tuple[str, ...],
                            regions: tuple[str, ...],
                            judge_names: tuple[str, ...]) -> None:
    """不可关联审计：复评单文本不得出现批号、产区、评委姓名。"""
    joined = "\n".join(
        [entry.redacted_excerpt for entry in sheet.entries]
        + list(sheet.open_doubts) + [entry.role for entry in sheet.entries]
    )
    leaks = []
    for token in (*lot_codes, *regions, *judge_names):
        if token and token in joined:
            leaks.append(token)
    # 同案多杯时，行序与角色不得让盲号可互相推断：角色只允许两个固定值
    roles = {entry.role for entry in sheet.entries}
    if not roles <= {"待复评", "对照杯"}:
        leaks.append(f"角色字段越界: {roles}")
    if leaks:
        raise AssertionError(f"复评单存在身份泄露线索: {leaks}")
    # 密钥必须能逐杯回收，且与复评单杯位集合一致
    sheet_cups = {entry.blind_cup for entry in sheet.entries}
    key_cups = {entry.blind_cup for entry in sealed.entries}
    if sheet_cups != key_cups:
        raise AssertionError("封存密钥与复评单杯位集合不一致")


def instruction_safe_for_frozen_delivery(text: str,
                                         delivery: DeliveryRecord) -> None:
    """送达后复核说明只能引用已冻结盲号，禁止出现任何新盲号对应。"""
    cups_in_text = set(re.findall(r"\b[A-Za-z]\d+\b", text))
    new_cups = cups_in_text - set(delivery.frozen_cups)
    if new_cups:
        raise PermissionError(
            f"盲审已送达，说明中出现未冻结的新盲号对应: {sorted(new_cups)}"
        )
