"""第一场（data/corpus.json）端到端与各条业务规则。"""
import unittest
from pathlib import Path

from materials import load_corpus
from review_flow.models import (
    AssertionKind,
    AssignmentStatus,
    ControlType,
    Polarity,
    ReviewPurpose,
    SenseType,
)
from review_flow.pipeline import ReviewPipeline, render_summary
from review_flow.sheets import instruction_safe_for_frozen_delivery

ROOT = Path(__file__).resolve().parent.parent


class SessionOneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus()
        cls.result = ReviewPipeline().run(cls.corpus)

    # ---- 短评定位：否定属性 ------------------------------------------
    def test_negated_attribute_located_to_sentence_and_cup(self):
        n1 = next(note for note in self.result.notes if note.record_id == "n1")
        self.assertEqual(n1.blind_cup, "H17")
        negated = [a for a in n1.assertions if a.polarity == Polarity.NEGATED]
        self.assertTrue(negated)
        defect_negation = next(a for a in negated if a.sense_code == "ferment_defect")
        self.assertEqual(defect_negation.blind_cup, "H17")
        self.assertEqual(defect_negation.sentence_index, 0)
        # 字符跨度必须能切回原句中的原词
        self.assertEqual(
            defect_negation.sentence[defect_negation.span[0]:defect_negation.span[1]],
            "ferment defect",
        )

    def test_chinese_negation_located(self):
        n3 = next(note for note in self.result.notes if note.record_id == "n3")
        no_winey = next(
            a for a in n3.assertions
            if a.sense_code == "winey_style" and a.polarity == Polarity.NEGATED
        )
        self.assertEqual(no_winey.blind_cup, "H17")
        self.assertIn("没有酒香", no_winey.sentence)

    # ---- 对照比较定位 ------------------------------------------------
    def test_comparison_references_control_cup(self):
        n1 = next(note for note in self.result.notes if note.record_id == "n1")
        comparison = next(a for a in n1.assertions
                          if a.kind == AssertionKind.COMPARISON)
        self.assertEqual(comparison.compared_cup, "Y04")
        n2 = next(note for note in self.result.notes if note.record_id == "n2")
        zh_comp = next(a for a in n2.assertions
                       if a.kind == AssertionKind.COMPARISON and a.compared_cup == "F23")
        self.assertEqual(zh_comp.blind_cup, "X52")

    def test_unregistered_reference_cup_becomes_doubt(self):
        # Y04 在分样记录里没有登记，不能当跨产区对照
        doubt = next(d for d in self.result.doubts if "对照杯来源未登记" in d.reason)
        self.assertIn("Y04", doubt.blind_cups)

    # ---- 主审引用 ----------------------------------------------------
    def test_head_judge_quote_is_separate_evidence(self):
        n2 = next(note for note in self.result.notes if note.record_id == "n2")
        quote = next(a for a in n2.assertions if a.kind == AssertionKind.HEAD_QUOTE)
        self.assertEqual(quote.quote_attribution, "主审")
        self.assertEqual(quote.term_text, "发酵")
        self.assertEqual(quote.sense_type, SenseType.UMBRELLA)
        doubt = next(d for d in self.result.doubts if "统一大类" in d.reason)
        self.assertEqual(doubt.raw_fragment, "发酵")
        # 引用主审用词不冒充描述人自己的属性判断
        self.assertFalse(
            any(a.kind == AssertionKind.ATTRIBUTE and a.term_text == "发酵"
                for a in n2.assertions)
        )

    # ---- 术语表：大类不能抵消 ----------------------------------------
    def test_umbrella_cannot_cancel_senses(self):
        glossary = self.result.glossary
        self.assertTrue(glossary.is_umbrella("fermentation"))
        self.assertTrue(glossary.share_umbrella("winey_style", "ferment_defect"))
        winey = glossary.get("winey_style")
        self.assertEqual(winey.conditional, "仅在洁净杯语境适用")

    def test_dispute_is_sense_collision_not_cancelled(self):
        dispute = next(d for d in self.result.disputes if d.lot_code == "K21")
        self.assertIn("winey_style", dispute.sense_codes)
        self.assertIn("ferment_defect", dispute.sense_codes)
        self.assertEqual(dispute.umbrella_codes, ("fermentation",))
        self.assertTrue(dispute.independent)

    # ---- 来源：改号同源、同来源留样 ----------------------------------
    def test_renumbered_cups_share_source(self):
        registry = self.result.provenance
        self.assertTrue(registry.is_same_source("H17", "X52"))
        lot = registry.require_lot_of_cup("X52")
        self.assertEqual(lot.lot_code, "K21")
        # 两个盲号都指向同一批号，任何一个都能收回同一来源
        self.assertEqual(
            registry.require_lot_of_cup("H17").lot_code,
            registry.require_lot_of_cup("X52").lot_code,
        )

    def test_same_source_retain_cup_not_cross_origin_control(self):
        registry = self.result.provenance
        controls, rejected = registry.eligible_controls(
            "K21", ControlType.KNOWN_DEFECT,
        )
        self.assertEqual([lot.lot_code for lot in controls], ["T18"])
        rejected_items = dict(rejected)
        # K21 自己的杯位（含任何留样）必须被剔除
        self.assertIn("H17", rejected_items)
        self.assertIn("X52", rejected_items)
        self.assertIn("同来源留样", rejected_items["H17"])
        # F23 用途不符：它是风格训练杯，不能充当缺陷对照
        self.assertIn("M08", rejected_items)

    # ---- 复评目的与资质 ----------------------------------------------
    def test_purpose_is_defect_confirmation(self):
        dispute = next(d for d in self.result.disputes if d.lot_code == "K21")
        self.assertEqual(dispute.recommended_purpose,
                         ReviewPurpose.DEFECT_CONFIRMATION)

    def test_assignment_recuses_promo_exposed_and_unqualified(self):
        assignment = next(a for a in self.result.assignments
                          if a.dispute_id == self.result.disputes[0].dispute_id)
        self.assertEqual(assignment.status, AssignmentStatus.ASSIGNED)
        self.assertEqual(assignment.judge.name, "秦禾")
        rejected = dict(assignment.rejected_judges)
        self.assertIn("孟萍", rejected)       # 见过 K21 宣传稿 → 改派
        self.assertIn("宣传稿", rejected["孟萍"])
        # 沈珂仅具备香型资质，对缺陷目的本身不胜任（换目的场景在第二场测试中验证）
        shenke = next(j for j in self.result.judges if j.name == "沈珂")
        self.assertNotIn(ReviewPurpose.DEFECT_CONFIRMATION, shenke.qualified_purposes)
        self.assertIn(ReviewPurpose.CALIBRATION, shenke.qualified_purposes)
        # 对照只能是缺陷用途的跨产区批次 T18
        self.assertEqual([lot.lot_code for lot in assignment.controls], ["T18"])

    # ---- 盲审单、封存密钥与不可关联 ----------------------------------
    def test_blind_sheet_redaction_and_sealed_key(self):
        sheet = self.result.sheet_for(self.result.disputes[0].dispute_id)
        self.assertIsNotNone(sheet)
        self.assertTrue(sheet.delivered)
        joined = "\n".join(e.redacted_excerpt for e in sheet.entries)
        for token in ("K21", "桐谷", "孟萍", "秦禾", "沈珂"):
            self.assertNotIn(token, joined)
        sealed = next(k for k in self.result.sealed_keys if k.sheet_id == sheet.sheet_id)
        entry = sealed.entry_for("H17")
        self.assertEqual(entry.lot_code, "K21")
        self.assertEqual(entry.region, "桐谷")
        # 原产区负责人可凭盲号收回来源证据
        self.assertIn("l1", entry.evidence_record_ids)
        # 盲审单只出现盲号与固定角色
        self.assertTrue({e.role for e in sheet.entries} <= {"待复评", "对照杯"})

    def test_sheet_contains_disputed_and_control_materials(self):
        sheet = self.result.sheet_for(self.result.disputes[0].dispute_id)
        roles = {entry.blind_cup: entry.role for entry in sheet.entries}
        # 争议涉及的同来源改号杯都作为待复评杯上盲审单
        self.assertEqual(roles.get("H17"), "待复评")
        self.assertEqual(roles.get("X52"), "待复评")
        # 仅适格跨产区对照可作对照杯；用途不符的 F23 不得进入
        self.assertEqual(roles.get("Z09"), "对照杯")
        self.assertNotIn("F23", roles)
        sealed = next(k for k in self.result.sealed_keys if k.sheet_id == sheet.sheet_id)
        self.assertEqual({e.blind_cup for e in sealed.entries}, set(roles))

    def test_frozen_delivery_rejects_new_cup_mapping(self):
        delivery = self.result.deliveries[0]
        with self.assertRaises(PermissionError):
            instruction_safe_for_frozen_delivery(
                "补充说明：新盲号 Q99 与 H17 同批，请一并核对", delivery,
            )
        # 只引用已冻结杯号的说明是允许的
        frozen = delivery.frozen_cups[0]
        instruction_safe_for_frozen_delivery(f"请复看 {frozen} 的回冷表现", delivery)

    # ---- 冲注修订 ----------------------------------------------------
    def test_different_pours_are_distinct_judgments(self):
        n3 = next(note for note in self.result.notes if note.record_id == "n3")
        self.assertEqual(n3.pour_seq, 2)
        n1 = next(note for note in self.result.notes if note.record_id == "n1")
        self.assertIsNone(n1.pour_seq)
        # 二冲注与首次冲注是不同判断，不得并入分封去重
        self.assertTrue(all("冲注" not in w for w in self.result.warnings))

    def test_summary_renders(self):
        text = render_summary(self.result)
        self.assertIn("送复评流程摘要", text)
        self.assertIn("秦禾", text)


if __name__ == "__main__":
    unittest.main()
