"""从语料记录到盲审复评单的可复用编排。

语料是管道符分隔的合成文本，适配层负责把记录翻译为领域对象；
领域引擎不依赖具体文本格式，下一场只换语料文件即可复用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from review_flow.models import (
    AssignmentStatus,
    ControlType,
    DeliveryRecord,
    Dispute,
    Doubt,
    Judge,
    PackagingEvent,
    ParsedNote,
    PromoExposure,
    ReviewAssignment,
    ReviewPurpose,
    ReviewSheet,
    SealedKey,
    SenseType,
    SourceLot,
    TermSense,
)
from review_flow.notes import NoteParser, detect_pour_seq
from review_flow.disputes import DisputeDetector
from review_flow.provenance import ProvenanceRegistry
from review_flow.review import ReviewPlanner
from review_flow.sheets import (
    SheetBuilder,
    assert_no_identity_leak,
    deliver,
    redact,
)
from review_flow.terminology import Glossary

# ----------------------------------------------------------------------
# 语料适配：glossary
# ----------------------------------------------------------------------
_WINEY_CLAUSE = re.compile(
    r"(winey|酒香|wine-like)[^;。；]*?(?:语境|context)[^;。；]*(?:香型|style)",
    re.IGNORECASE,
)
_DEFECT_CLAUSE = re.compile(r"ferment defect|发酵缺陷", re.IGNORECASE)
_UMBRELLA_CLAUSE = re.compile(r"[“\"]?发酵[”\"]?")


def parse_glossary_record(record: dict, glossary: Glossary) -> None:
    """把词义说明记录登记为：两个具体义项 + 一个仅检索用统一大类。"""
    text = record["text"]
    umbrella = TermSense(
        code="fermentation", canonical="发酵（统一检索大类，不可用于抵消意见）",
        sense_type=SenseType.UMBRELLA,
        languages={"zh": ("发酵",)}, glossary_record_id=record["id"],
        revision=glossary.revision,
    )
    winey_forms = {"en": ("winey",), "zh": ("酒香",)}
    defect_forms = {"en": ("ferment defect",), "zh": ("发酵缺陷",)}
    if not _WINEY_CLAUSE.search(text):
        winey_forms = {}
    if not _DEFECT_CLAUSE.search(text):
        defect_forms = {}

    senses = []
    if winey_forms:
        senses.append(TermSense(
            code="winey_style", canonical="发酵带来的酒香风格（香型义项）",
            sense_type=SenseType.SENSE, umbrella_codes=("fermentation",),
            languages=winey_forms, conditional="仅在洁净杯语境适用",
            glossary_record_id=record["id"], revision=glossary.revision,
        ))
    if defect_forms:
        senses.append(TermSense(
            code="ferment_defect", canonical="发酵缺陷（缺陷义项）",
            sense_type=SenseType.SENSE, umbrella_codes=("fermentation",),
            languages=defect_forms,
            glossary_record_id=record["id"], revision=glossary.revision,
        ))
    for sense in (umbrella, *senses):
        glossary.add_sense(sense)


# ----------------------------------------------------------------------
# 语料适配：lot / 分封 / 宣传稿接触
# ----------------------------------------------------------------------
_LOT_HEADER = re.compile(r"^(?P<name>[^|]+)\|批号\s*(?P<code>[A-Z]\d+)[，,]盲样\s*(?P<cups>[A-Za-z0-9、, ]+)")
_RENUMBER = re.compile(r"改盲号(?:为|到)\s*(?P<new>[A-Za-z]\d+)[^；;。]*?原盲号\s*(?P<old>[A-Za-z]\d+)")
_EVENT_CUPS = re.compile(r"盲样\s*([A-Za-z0-9、, ]+?)(?:[。；;]|$)")
_PROMO = re.compile(r"评委(?P<judge>[一-鿿]{2,4})校过\s*(?P<code>[A-Z]\d+)\s*产地宣传稿")


def parse_lot_record(record: dict, registry: ProvenanceRegistry) -> list[PromoExposure]:
    text = record["text"]
    header = _LOT_HEADER.search(text)
    if not header:
        raise ValueError(f"批号记录格式无法识别: {record['id']}")
    name = header.group("name").strip()
    code = header.group("code")
    region = name[:2]
    cups = tuple(re.findall(r"[A-Za-z]\d+", header.group("cups")))

    control_type = None
    if "缺陷" in text and "对照" in text:
        control_type = ControlType.KNOWN_DEFECT
    elif "训练杯" in text or "洁净" in text:
        control_type = ControlType.CLEAN_STYLE
    if "同来源留样" in text:
        control_type = ControlType.SAME_ORIGIN_RETAIN

    lot = SourceLot(lot_code=code, name=name, region=region,
                    blind_cups=list(cups), control_type=control_type,
                    record_id=record["id"])
    registry.add_lot(lot)
    event = PackagingEvent(
        event_id=f"E-{code}-0", lot_code=code, blind_cups=cups,
        record_id=record["id"], note="初始分封",
    )
    registry.add_event(event)

    renumber = _RENUMBER.search(text)
    if renumber:
        new_cup = renumber.group("new")
        old_cup = renumber.group("old")
        registry.add_event(PackagingEvent(
            event_id=f"E-{code}-renumber", lot_code=code,
            blind_cups=(new_cup,), record_id=record["id"],
            note="同一小批改盲号再分封，来源不变",
            re_numbered_from=(old_cup,),
        ))

    exposures: list[PromoExposure] = []
    promo = _PROMO.search(text)
    if promo:
        exposures.append(PromoExposure(
            judge=promo.group("judge"), lot_code=promo.group("code"),
            record_id=record["id"], detail="校过产地宣传稿",
        ))
    return exposures


# ----------------------------------------------------------------------
# 语料适配：roster
# ----------------------------------------------------------------------
_ROSTER_HEADER = re.compile(r"复评席\|(.+)")
_JUDGE_CLAUSE = re.compile(
    r"(?P<judge>[一-鿿]{2,4})(?:可做|能做|仅能|仅)(?P<skills>[^。；;]*)"
)


def parse_roster_record(record: dict) -> tuple[list[Judge], tuple[str, ...]]:
    text = record["text"]
    body = _ROSTER_HEADER.search(text).group(1)
    judges: list[Judge] = []
    held: tuple[str, ...] = ()
    for clause in re.split(r"[。；;]", body):
        clause = clause.strip()
        if not clause:
            continue
        if "领取" in clause or "领单" in clause:
            # 记录被领取的送审单编号；领单人在下面按已解析评委回填
            held = tuple(re.findall(r"S-[A-Z0-9-]+", clause))
            if not held:
                held = tuple(re.findall(r"[一-鿿]{2,4}", clause))
            continue
        match = _JUDGE_CLAUSE.search(clause)
        if not match:
            continue
        name = match.group("judge")
        skills = match.group("skills")
        purposes: list[ReviewPurpose] = []
        if "香型" in skills:
            purposes.append(ReviewPurpose.CALIBRATION)
        if "缺陷" in skills:
            purposes.append(ReviewPurpose.DEFECT_CONFIRMATION)
        judges.append(Judge(name=name, qualified_purposes=tuple(purposes),
                            record_id=record["id"]))
    return judges, held


# ----------------------------------------------------------------------
# 语料适配：note
# ----------------------------------------------------------------------
_NOTE_HEADER = re.compile(r"^(?P<judge>[^|]+)\s+(?P<cup>[A-Za-z]\d+)\|(?P<body>.+)$")


def parse_note_record(record: dict, parser: NoteParser) -> ParsedNote:
    match = _NOTE_HEADER.search(record["text"])
    if not match:
        raise ValueError(f"短评记录格式无法识别: {record['id']}")
    judge = match.group("judge").strip()
    cup = match.group("cup")
    body = match.group("body").strip()
    pour_seq = detect_pour_seq(body)
    return parser.parse(record["id"], judge, cup, body, pour_seq=pour_seq)


# ----------------------------------------------------------------------
# 修订记录（如二冲注不是同一次判断）
# ----------------------------------------------------------------------
def apply_revision_records(records: list[dict],
                           notes: list[ParsedNote]) -> list[str]:
    warnings: list[str] = []
    note_index = {note.record_id: note for note in notes}
    for record in records:
        if record["kind"] != "revision":
            continue
        text = record["text"]
        ref_ids = re.findall(r"n\d+", text)
        if "不得重复登记分封" in text:
            # 分封去重由来源注册表保证；此处仅核对所引短评确实同属一个已登记批号
            for ref in ref_ids:
                note = note_index.get(ref)
                if note is None:
                    warnings.append(f"{record['id']}: 修订记录引用了不存在的短评 {ref}")
        elif "不是同一次判断" in text:
            # 同一杯位的不同冲注必须保留为不同判断，不得合并成新批次
            pour_seqs = {note_index[ref].pour_seq for ref in ref_ids if ref in note_index}
            if len(pour_seqs) < 2:
                warnings.append(f"{record['id']}: 修订记录要求区分冲注，但短评未体现不同冲注序")
    return warnings


# ----------------------------------------------------------------------
# 流水线
# ----------------------------------------------------------------------
@dataclass
class PipelineResult:
    glossary: Glossary
    provenance: ProvenanceRegistry
    notes: list[ParsedNote]
    judges: list[Judge]
    disputes: list[Dispute]
    doubts: list[Doubt]
    assignments: list[ReviewAssignment]
    sheets: list[ReviewSheet]
    sealed_keys: list[SealedKey]
    deliveries: list[DeliveryRecord]
    warnings: list[str] = field(default_factory=list)

    def sheet_for(self, dispute_id: str) -> Optional[ReviewSheet]:
        for sheet in self.sheets:
            if sheet.assignment_id == dispute_id:
                return sheet
        return None


class ReviewPipeline:
    """送复评流水线。同一实例可加载多场语料，每场独立调用 run()。"""

    def run(self, corpus: dict,
            purpose_override: ReviewPurpose | None = None,
            redesignation: tuple[str, str, str] | None = None
            ) -> PipelineResult:
        records = corpus["records"]
        glossary = Glossary(revision="v1")
        registry = ProvenanceRegistry()
        exposures: list[PromoExposure] = []
        judges: list[Judge] = []
        held_sheets: set[str] = set()
        note_records: list[dict] = []

        for record in records:
            if record["kind"] == "glossary":
                parse_glossary_record(record, glossary)
            elif record["kind"] == "lot":
                exposures.extend(parse_lot_record(record, registry))
            elif record["kind"] == "roster":
                judges, held = parse_roster_record(record)
                held_sheets.update(held)
            elif record["kind"] == "note":
                note_records.append(record)

        # 重新指定词义（词面, 目标义项, 新版本号）→ 目的可能随之改变
        redesignation_note = ""
        if redesignation is not None:
            term_text, target_code, new_revision = redesignation
            glossary = glossary.redesignate(term_text, target_code, new_revision)
            redesignation_note = (
                f"词义重新指定：{term_text} → {target_code}（{new_revision}），"
                "复评目的、评委资质与对照杯按新义项重算"
            )

        parser = NoteParser(glossary)
        notes = [parse_note_record(record, parser) for record in note_records]
        warnings = apply_revision_records(records, notes)
        if redesignation_note:
            warnings.append(redesignation_note)

        for exposure in exposures:
            registry.add_exposure(exposure)
        for judge in judges:
            judge.exposures = [e for e in exposures if e.judge == judge.name]
        # 领单回填：定位“领取/领单”子句，只把送审单挂到该子句点名的评委名下
        if held_sheets:
            hold_clause = next(
                (clause for record in records if record["kind"] == "roster"
                 for clause in re.split(r"[。；;]", record["text"])
                 if any(sheet_id in clause for sheet_id in held_sheets)),
                "",
            )
            for judge in judges:
                if judge.name in hold_clause:
                    judge.holds_sheets = tuple(sorted(held_sheets))

        detector = DisputeDetector(glossary, registry)
        detection = detector.detect(notes)

        planner = ReviewPlanner(registry, judges)
        assignments = [
            planner.replan_after_redesignation(
                dispute, purpose_override or dispute.recommended_purpose,
                held_sheet_ids=held_sheets,
            ) if redesignation
            else planner.plan(dispute, purpose_override)
            for dispute in detection.disputes
        ]

        judge_names = tuple(j.name for j in judges)
        builder = SheetBuilder(registry, judge_names=judge_names)
        sheets: list[ReviewSheet] = []
        sealed_keys: list[SealedKey] = []
        deliveries: list[DeliveryRecord] = []
        lot_codes = tuple(lot.lot_code for lot in registry.lots)
        regions = tuple(dict.fromkeys(lot.region for lot in registry.lots))

        for dispute, assignment in zip(detection.disputes, assignments):
            if assignment.status != AssignmentStatus.ASSIGNED or assignment.judge is None:
                continue
            sheet, sealed = builder.build(assignment, dispute)
            assert_no_identity_leak(sheet, sealed, lot_codes, regions, judge_names)
            delivery = deliver(sheet)
            planner.mark_locked(sheet.sheet_id)
            sheets.append(sheet)
            sealed_keys.append(sealed)
            deliveries.append(delivery)

        return PipelineResult(
            glossary=glossary, provenance=registry, notes=notes,
            judges=judges, disputes=detection.disputes,
            doubts=detection.doubts, assignments=assignments,
            sheets=sheets, sealed_keys=sealed_keys,
            deliveries=deliveries, warnings=warnings,
        )


def load_corpus(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def render_summary(result: PipelineResult) -> str:
    """生成给秘书看的中文流程摘要（不入盲审单）。"""
    lines = ["== 送复评流程摘要 =="]
    for dispute in result.disputes:
        lines.append(
            f"[{dispute.dispute_id}] 批号 {dispute.lot_code} 杯位 {','.join(dispute.blind_cups)}"
        )
        lines.append(f"  认定：{dispute.classification}｜独立分歧={dispute.independent}")
        lines.append(f"  建议目的：{dispute.recommended_purpose.value}")
    regions = tuple(dict.fromkeys(lot.region for lot in result.provenance.lots))
    lot_codes = tuple(lot.lot_code for lot in result.provenance.lots)
    judge_names = tuple(j.name for j in result.judges)
    for assignment in result.assignments:
        lines.append(
            f"派单 {assignment.dispute_id}: {assignment.status.value}｜"
            f"目的={assignment.purpose.value}｜"
            f"评委={assignment.judge.name if assignment.judge else '—'}｜"
            f"对照={','.join(lot.lot_code for lot in assignment.controls) or '—'}"
        )
        for judge_name, reason in assignment.rejected_judges:
            lines.append(f"  剔除评委 {judge_name}: {reason}")
        for cup_or_lot, reason in assignment.rejected_controls:
            lines.append(f"  剔除对照 {cup_or_lot}: {redact(reason, judge_names, regions, lot_codes)}")
        if assignment.reason:
            lines.append(f"  说明：{assignment.reason}")
    if not result.assignments:
        lines.append("本场未认定需要送复评的独立分歧。")
    for doubt in result.doubts:
        lines.append(
            f"疑点 {doubt.doubt_id}: {doubt.reason}｜记录={','.join(doubt.record_ids)}"
            f"｜原片段={doubt.raw_fragment or '—'}"
        )
    for sheet in result.sheets:
        lines.append(f"盲审单 {sheet.sheet_id}（{sheet.judge_name}，{sheet.purpose.value}）:")
        for entry in sheet.entries:
            lines.append(f"  {entry.entry_no}. {entry.blind_cup} [{entry.role}] {entry.redacted_excerpt}")
    for warning in result.warnings:
        lines.append(f"流程提示：{warning}")
    return "\n".join(lines)
