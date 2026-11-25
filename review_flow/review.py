"""复评派单。

目的驱动，规则联动：
- 校准香型 → 仅香型资质评委 + 洁净风格跨产区对照杯；
- 确认缺陷 → 缺陷资质评委 + 已确认缺陷跨产区对照杯；
- 重新指定词义使目的改变时，资质与对照必须重新匹配，原派单不再沿用。
利益冲突：
- 评委见过同来源批次宣传稿 → 改派；无合格人则退回不足。
对照约束：
- 同来源留样、同产区批次、用途不符的批次一律剔除；
- 对照不足同样退回不足，不得用来源不明杯凑数。
冻结保护：
- 已送达锁定且仍合格的其他场次不换；换目的导致不再合格的场次必须重派，
  且新说明不得暴露已冻结盲号的新对应关系（见 sheets）。
"""
from __future__ import annotations

from review_flow.models import (
    AssignmentStatus,
    ControlType,
    Dispute,
    Judge,
    ReviewAssignment,
    ReviewPurpose,
)
from review_flow.provenance import ProvenanceRegistry

PURPOSE_CONTROLS = {
    ReviewPurpose.CALIBRATION: ControlType.CLEAN_STYLE,
    ReviewPurpose.DEFECT_CONFIRMATION: ControlType.KNOWN_DEFECT,
}


class ReviewPlanner:
    def __init__(self, provenance: ProvenanceRegistry,
                 judges: list[Judge]) -> None:
        self.provenance = provenance
        self.judges = judges
        self.locked_sheet_ids: set[str] = set()

    def mark_locked(self, sheet_id: str) -> None:
        self.locked_sheet_ids.add(sheet_id)

    def plan(self, dispute: Dispute,
             purpose: ReviewPurpose | None = None) -> ReviewAssignment:
        purpose = purpose or dispute.recommended_purpose
        assignment = ReviewAssignment(
            dispute_id=dispute.dispute_id, purpose=purpose,
            status=AssignmentStatus.INSUFFICIENT,
            doubts=list(dispute.doubts),
        )

        if not dispute.independent:
            assignment.reason = "改号转记重复，不构成独立分歧，无需另立复评席"
            assignment.status = AssignmentStatus.INSUFFICIENT
            return assignment

        # 1) 对照杯先定，避免人合格却无适格材料
        required_control = PURPOSE_CONTROLS[purpose]
        controls, rejected_controls = self.provenance.eligible_controls(
            dispute.lot_code, required_control,
        )
        assignment.rejected_controls = rejected_controls
        if not controls:
            assignment.reason = (
                f"缺少 {purpose.value} 所需的跨产区对照（{required_control.value}），退回补足材料"
            )
            return assignment

        # 2) 评委资质 + 利益冲突
        candidate: Judge | None = None
        for judge in self.judges:
            if purpose not in judge.qualified_purposes:
                assignment.rejected_judges.append(
                    (judge.name, f"不具备 {purpose.value} 资质")
                )
                continue
            exposure = self.provenance.exposure_for(judge.name, dispute.lot_code)
            if exposure is not None:
                assignment.rejected_judges.append(
                    (judge.name, f"见过批号 {dispute.lot_code} 宣传稿（{exposure.record_id}），改派"),
                )
                continue
            candidate = judge
            break

        if candidate is None:
            assignment.reason = "无适格且无利益冲突的评委，退回不足"
            return assignment

        assignment.judge = candidate
        assignment.controls = controls
        assignment.status = AssignmentStatus.ASSIGNED
        assignment.reason = (
            f"按 {purpose.value} 派给 {candidate.name}；"
            f"对照 {', '.join(lot.lot_code for lot in controls)}"
        )
        return assignment

    def replan_after_redesignation(self, dispute: Dispute,
                                   new_purpose: ReviewPurpose,
                                   held_sheet_ids: set[str] | None = None
                                   ) -> ReviewAssignment:
        """重新指定词义后的重派：资质与对照随目的重新匹配。"""
        held_sheet_ids = held_sheet_ids or set()
        # 先把旧领单人的新目的资质结论记下，即使新单另派他人也保留留痕
        prior_holders = [
            judge for judge in self.judges
            if any(sheet_id in held_sheet_ids for sheet_id in judge.holds_sheets)
        ]
        pre_rejections: list[tuple[str, str]] = []
        for judge in prior_holders:
            if new_purpose not in judge.qualified_purposes:
                pre_rejections.append(
                    (judge.name, "原送审单领单人，换目的后资质不再匹配，旧单冻结追回"),
                )

        assignment = self.plan(dispute, new_purpose)
        existing = dict(assignment.rejected_judges)
        for name, reason in pre_rejections:
            existing.setdefault(name, reason)
        assignment.rejected_judges = list(existing.items())
        return assignment

    def locked_sessions_still_valid(self, assignments: list[ReviewAssignment]
                                    ) -> list[str]:
        """已锁定且仍合格的场次不需要换，返回可保留的复评单编号。"""
        return [
            sheet_id for sheet_id in self.locked_sheet_ids
            if any(a.status == AssignmentStatus.LOCKED
                   and a.judge is not None for a in assignments)
        ]
