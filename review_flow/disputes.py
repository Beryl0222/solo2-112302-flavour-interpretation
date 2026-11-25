"""分歧认定。

规则落点：
- 统一大类只用于检索：同属“发酵”大类的两个具体义项（winey 香型 / ferment 缺陷）
  不能被大类直接抵消，须按义项分别立证。
- 同一小批改号后的相同意见是转记重复：同一评委、同一冲注序、同来源不同盲号的
  相同意见合并，不构成独立分歧（不同冲注是不同判断，按修订记录不合并）。
- 未登记词面、裸用大类词、对照杯来源不明都先登记疑点，允许秘书查原语言片段。
- 争议涉及缺陷义项时，建议目的为“确认缺陷”，否则为“校准香型”。
"""
from __future__ import annotations

from dataclasses import dataclass

from review_flow.models import (
    Assertion,
    AssertionKind,
    Dispute,
    Doubt,
    OpinionGroup,
    ParsedNote,
    ReviewPurpose,
    SenseType,
)
from review_flow.provenance import ProvenanceRegistry, UnknownCupError
from review_flow.terminology import Glossary

CLASSIFICATION_SENSE_COLLISION = "同大类下义项分歧"
CLASSIFICATION_POLARITY = "同义项极性冲突"
CLASSIFICATION_DUPLICATE = "改号转记重复，不构成独立分歧"


def is_defect_sense(glossary: Glossary, sense_code: str | None) -> bool:
    if not sense_code or not glossary.has(sense_code):
        return False
    sense = glossary.get(sense_code)
    return sense.code.endswith("_defect") or "缺陷" in sense.canonical


@dataclass
class DetectionResult:
    disputes: list[Dispute]
    doubts: list[Doubt]
    groups: list[OpinionGroup]


class DisputeDetector:
    def __init__(self, glossary: Glossary, provenance: ProvenanceRegistry) -> None:
        self.glossary = glossary
        self.provenance = provenance
        self.doubts: list[Doubt] = []
        self.all_assertions: list[Assertion] = []
        self._doubt_seq = 0

    def _new_doubt(self, reason: str, record_ids: tuple[str, ...],
                   blind_cups: tuple[str, ...] = (),
                   raw_fragment: str | None = None, detail: str = "") -> Doubt:
        self._doubt_seq += 1
        doubt = Doubt(
            doubt_id=f"d{self._doubt_seq}", reason=reason,
            record_ids=record_ids, blind_cups=blind_cups,
            raw_fragment=raw_fragment, detail=detail,
        )
        self.doubts.append(doubt)
        return doubt

    def detect(self, notes: list[ParsedNote]) -> DetectionResult:
        assertions = self._collect(notes)
        groups = self._group(assertions)
        disputes = self._build_disputes(groups)
        return DetectionResult(disputes=disputes, doubts=list(self.doubts),
                               groups=groups)

    # ------------------------------------------------------------------
    def _collect(self, notes: list[ParsedNote]) -> list[Assertion]:
        collected: list[Assertion] = []
        for note in notes:
            for assertion in note.assertions:
                try:
                    self.provenance.require_lot_of_cup(assertion.blind_cup)
                except UnknownCupError:
                    self._new_doubt(
                        "杯位未在分样记录中登记，来源不明",
                        (assertion.record_id,), (assertion.blind_cup,),
                        raw_fragment=assertion.sentence,
                    )
                if assertion.sense_type == SenseType.UNRESOLVED:
                    if not assertion.term_text:
                        # 纯比较占位句：没有可解析词面，比较来源在下面单独核查
                        pass
                    else:
                        self._new_doubt(
                            "词面在对照表中缺少区分证据，复评前先留疑点，可查原语言片段",
                            (assertion.record_id,), (assertion.blind_cup,),
                            raw_fragment=assertion.term_text or assertion.sentence,
                        )
                elif assertion.sense_type == SenseType.UMBRELLA:
                    self._new_doubt(
                        "仅命中统一大类，未指定具体义项，不得以大类抵消双方意见",
                        (assertion.record_id,), (assertion.blind_cup,),
                        raw_fragment=assertion.term_text,
                    )
                if assertion.kind == AssertionKind.COMPARISON and assertion.compared_cup:
                    if self.provenance.lot_of_cup(assertion.compared_cup) is None:
                        self._new_doubt(
                            "比较所引对照杯来源未登记，不能据此安排跨产区对照",
                            (assertion.record_id,),
                            (assertion.blind_cup, assertion.compared_cup),
                            raw_fragment=assertion.sentence,
                        )
                collected.append(assertion)
        self.all_assertions = collected
        return collected

    def _group(self, assertions: list[Assertion]) -> list[OpinionGroup]:
        buckets: dict[str, OpinionGroup] = {}
        for assertion in assertions:
            lot = self.provenance.lot_of_cup(assertion.blind_cup)
            lot_code = lot.lot_code if lot else "UNKNOWN"
            # 只有解析到具体义项的意见参与分组；主审引用单独成证，不混入描述人判断
            if assertion.kind == AssertionKind.HEAD_QUOTE:
                continue
            if assertion.sense_type != SenseType.SENSE or not assertion.sense_code:
                continue  # 疑点已登记；未解析词不参与分组对立
            key = "|".join([
                lot_code, assertion.sense_code,
                assertion.polarity.value, assertion.kind.value,
            ])
            group = buckets.get(key)
            if group is None:
                group = OpinionGroup(
                    key=key, lot_code=lot_code, sense_code=assertion.sense_code,
                    sense_type=assertion.sense_type, polarity=assertion.polarity,
                    kind=assertion.kind,
                )
                buckets[key] = group
            if self._is_retranscription(group, assertion):
                group.merged_duplicates.append(assertion)
            else:
                group.assertions.append(assertion)
        return list(buckets.values())

    def _is_retranscription(self, group: OpinionGroup, assertion: Assertion) -> bool:
        """同评委、同冲注序、同来源不同盲号的相同意见 = 转记重复。

        不同冲注序（如二冲注复核）按修订记录属于另一次判断，不合并。
        """
        if assertion.kind != AssertionKind.ATTRIBUTE:
            return False
        for existing in (*group.assertions, *group.merged_duplicates):
            if existing.judge != assertion.judge:
                continue
            if existing.pour_seq != assertion.pour_seq:
                continue
            if existing.blind_cup == assertion.blind_cup:
                continue
            if self.provenance.is_same_source(existing.blind_cup, assertion.blind_cup):
                return True
        return False

    # ------------------------------------------------------------------
    def _build_disputes(self, groups: list[OpinionGroup]) -> list[Dispute]:
        # 主审引用不进分组，其余带具体义项的意见（属性与比较）都参与聚类
        attr_groups = [g for g in groups
                       if g.kind != AssertionKind.HEAD_QUOTE
                       and g.lot_code != "UNKNOWN"]
        # 聚类键：同来源 + 同统一大类（无大类则按具体义项）。
        # 同大类下的所有对立只立一个争议，大类本身不参与抵消。
        clusters: dict[tuple[str, str], list[OpinionGroup]] = {}
        for group in attr_groups:
            umbrellas = self.glossary.get(group.sense_code).umbrella_codes
            cluster_key = (group.lot_code, umbrellas[0] if umbrellas else f"sense:{group.sense_code}")
            clusters.setdefault(cluster_key, []).append(group)

        disputes: list[Dispute] = []
        seq = 0
        for (lot_code, _), cluster in sorted(clusters.items()):
            if not self._cluster_conflicts(cluster):
                continue
            seq += 1
            disputes.append(self._make_dispute(seq, cluster))
        return disputes

    def _cluster_conflicts(self, cluster: list[OpinionGroup]) -> bool:
        for index, left in enumerate(cluster):
            for right in cluster[index + 1:]:
                if self._conflicts(left, right):
                    return True
        return False

    def _conflicts(self, left: OpinionGroup, right: OpinionGroup) -> bool:
        if left.sense_code == right.sense_code:
            return left.polarity != right.polarity
        return self.glossary.share_umbrella(left.sense_code, right.sense_code)

    def _make_dispute(self, seq: int, cluster: list[OpinionGroup]) -> Dispute:
        same_sense = len({g.sense_code for g in cluster}) == 1
        classification = (
            CLASSIFICATION_POLARITY if same_sense
            else CLASSIFICATION_SENSE_COLLISION
        )
        independent = self._independent(cluster)
        if not independent:
            classification = CLASSIFICATION_DUPLICATE

        cups = tuple(dict.fromkeys(
            assertion.blind_cup for group in cluster
            for assertion in (*group.assertions, *group.merged_duplicates)
        ))
        sense_codes = tuple(dict.fromkeys(g.sense_code for g in cluster))
        umbrellas = tuple(dict.fromkeys(
            code for group in cluster
            for code in self.glossary.get(group.sense_code).umbrella_codes
        ))
        purpose = (
            ReviewPurpose.DEFECT_CONFIRMATION
            if any(is_defect_sense(self.glossary, code) for code in sense_codes)
            else ReviewPurpose.CALIBRATION
        )
        lot = self.provenance.require_lot_of_cup(cups[0])
        excluded = [cup for cup in lot.blind_cups if cup not in cups]

        evidence = self._evidence_for_lot(lot.lot_code)
        dispute = Dispute(
            dispute_id=f"p{seq}", lot_code=lot.lot_code, blind_cups=cups,
            sense_codes=sense_codes, umbrella_codes=umbrellas,
            groups=list(cluster), classification=classification,
            recommended_purpose=purpose, independent=independent,
            evidence_assertions=evidence, excluded_controls=excluded,
        )
        dispute.doubts = [
            doubt for doubt in self.doubts
            if any(cup in dispute.blind_cups for cup in doubt.blind_cups)
        ]
        if not independent:
            merged = [
                assertion for group in cluster
                for assertion in group.merged_duplicates
            ]
            dispute.merged_note = (
                "同来源改号杯上的相同意见按转记重复合并："
                + "；".join(
                    f"{a.judge} 在 {a.blind_cup} 的意见转记自同批盲号"
                    for a in merged
                )
            )
        return dispute

    def _independent(self, cluster: list[OpinionGroup]) -> bool:
        """只要存在两位不同评委的实质对立，就是独立分歧。

        同一评委、同一冲注序、同来源改号杯之间的对立视为转记重复，
        不单独构成独立分歧；不同冲注序是不同判断，仍算独立。
        """
        substantive = [
            assertion for group in cluster for assertion in group.assertions
        ]
        judges = {a.judge for a in substantive}
        if len(judges) >= 2:
            return True
        for index, left in enumerate(substantive):
            for right in substantive[index + 1:]:
                if left.pour_seq != right.pour_seq:
                    return True
                if not self.provenance.is_same_source(left.blind_cup, right.blind_cup):
                    return True
        return False

    def _evidence_for_lot(self, lot_code: str) -> list[Assertion]:
        """汇总该来源上的全部可定位证据，含比较句与主审引用。"""
        return [a for a in self.all_assertions
                if (self.provenance.lot_of_cup(a.blind_cup)
                    and self.provenance.lot_of_cup(a.blind_cup).lot_code == lot_code)]
