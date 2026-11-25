"""第二场（data/session_two.json）与跨场次规则。"""
import unittest
from pathlib import Path

from review_flow.models import (
    AssertionKind,
    AssignmentStatus,
    ControlType,
    Polarity,
    ReviewPurpose,
    SenseType,
)
from review_flow.pipeline import ReviewPipeline, load_corpus
from review_flow.sheets import assert_no_identity_leak

ROOT = Path(__file__).resolve().parent.parent


class SessionTwoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(ROOT / "data" / "session_two.json")
        cls.result = ReviewPipeline().run(cls.corpus)

    # ---- 改号同源与分封事件 ------------------------------------------
    def test_renumber_event_preserved(self):
        registry = self.result.provenance
        lot = registry.require_lot_of_cup("B22")
        self.assertEqual(lot.lot_code, "B07")
        event = next(e for e in lot.packaging_events if e.re_numbered_from)
        self.assertEqual(event.re_numbered_from, ("A11",))
        self.assertIn("B22", event.blind_cups)

    # ---- 两次转记的相同意见不构成独立分歧 ----------------------------
    def test_retranscribed_duplicate_merged(self):
        # 评委丁在 A11 与 B22 上的 winey 肯定意见同评委同冲注同来源 → 合并
        groups = [g for g in self.result.disputes[0].groups]
        winey_affirmed = next(
            g for g in groups
            if g.sense_code == "winey_style" and g.polarity == Polarity.AFFIRMED
        )
        self.assertEqual(len(winey_affirmed.assertions), 1)
        self.assertEqual(len(winey_affirmed.merged_duplicates), 1)
        cups = {a.blind_cup for a in
                winey_affirmed.assertions + winey_affirmed.merged_duplicates}
        self.assertEqual(cups, {"A11", "B22"})

        # 但评委己与评委丁是两方独立判断，争议整体仍然独立成立
        dispute = self.result.disputes[0]
        self.assertTrue(dispute.independent)
        judges = {a.judge for g in dispute.groups for a in g.assertions}
        self.assertEqual(judges, {"评委丁", "评委己"})

    # ---- 同来源留样不能当跨产区对照 ----------------------------------
    def test_same_origin_retain_excluded(self):
        registry = self.result.provenance
        controls, rejected = registry.eligible_controls(
            "B07", ControlType.KNOWN_DEFECT,
        )
        self.assertEqual([lot.lot_code for lot in controls], ["D10"])
        rejected_map = dict(rejected)
        self.assertIn("S88", rejected_map)
        self.assertIn("同来源留样", rejected_map["S88"])
        # 风格训练杯用途不符，不能充当缺陷对照
        self.assertIn("C09", rejected_map)

    # ---- 缺少区分证据的词先留疑点 ------------------------------------
    def test_unresolved_term_kept_as_doubt_with_raw_fragment(self):
        doubt = next(d for d in self.result.doubts if "区分证据" in d.reason)
        self.assertEqual(doubt.raw_fragment, "muddled finish")
        self.assertIn("n7", doubt.record_ids)
        # 该词不被强行塞进任何义项分组
        note = next(n for n in self.result.notes if n.record_id == "n7")
        unresolved = [a for a in note.assertions
                      if a.sense_type == SenseType.UNRESOLVED and a.term_text]
        self.assertTrue(unresolved)

    # ---- 宣传稿接触改派 ----------------------------------------------
    def test_promo_exposure_forces_reassignment(self):
        dispute = self.result.disputes[0]
        assignment = next(a for a in self.result.assignments
                          if a.dispute_id == dispute.dispute_id)
        self.assertEqual(assignment.status, AssignmentStatus.ASSIGNED)
        self.assertEqual(assignment.judge.name, "白澄")
        rejected = dict(assignment.rejected_judges)
        self.assertIn("韩洲", rejected)
        self.assertIn("宣传稿", rejected["韩洲"])

    # ---- 盲审单脱敏与密钥回收 ----------------------------------------
    def test_sheet_unlinkability_and_key_recovery(self):
        dispute = self.result.disputes[0]
        sheet = self.result.sheet_for(dispute.dispute_id)
        sealed = next(k for k in self.result.sealed_keys if k.sheet_id == sheet.sheet_id)
        lot_codes = tuple(lot.lot_code for lot in self.result.provenance.lots)
        regions = tuple(dict.fromkeys(lot.region for lot in self.result.provenance.lots))
        judge_names = tuple(j.name for j in self.result.judges)
        assert_no_identity_leak(sheet, sealed, lot_codes, regions, judge_names)
        entry = sealed.entry_for("B22")
        self.assertEqual(entry.lot_code, "B07")
        self.assertEqual(entry.region, "云岭")
        self.assertIn("l4", entry.evidence_record_ids)

    # ---- 重指定词义：目的、资质、对照联动 ----------------------------
    def test_redesignation_switches_purpose_and_rechecks_eligibility(self):
        # 把裸“发酵”重新指定为缺陷义项（v2），原香型领单人陆舟不再合格
        result_v2 = ReviewPipeline().run(
            self.corpus,
            redesignation=("发酵", "ferment_defect", "v2"),
        )
        self.assertEqual(result_v2.glossary.revision, "v2")
        self.assertIn(("v2", "发酵", "ferment_defect"), result_v2.glossary.history)
        dispute = result_v2.disputes[0]
        assignment = next(a for a in result_v2.assignments
                          if a.dispute_id == dispute.dispute_id)
        self.assertEqual(assignment.purpose, ReviewPurpose.DEFECT_CONFIRMATION)
        rejected = dict(assignment.rejected_judges)
        self.assertIn("陆舟", rejected)
        self.assertIn("资质不再匹配", rejected["陆舟"])
        # 对照杯也换成缺陷用途的 D10
        self.assertEqual([lot.lot_code for lot in assignment.controls], ["D10"])
        # 新盲审单里不出现任何旧单标识
        sheet = result_v2.sheet_for(dispute.dispute_id)
        joined = "\n".join(e.redacted_excerpt for e in sheet.entries)
        self.assertNotIn("S-OLD", joined)


class PurposeControlTest(unittest.TestCase):
    def test_calibration_requires_clean_style_control(self):
        corpus = load_corpus(ROOT / "data" / "session_two.json")
        result = ReviewPipeline().run(corpus,
                                      purpose_override=ReviewPurpose.CALIBRATION)
        assignment = result.assignments[0]
        # B07 争议被强制按校准处理时，需要 C09 风格训练杯
        self.assertEqual([lot.lot_code for lot in assignment.controls], ["C09"])
        # 香型目的下韩洲仍因宣传稿被改派，陆舟可接
        self.assertEqual(assignment.judge.name, "陆舟")


class LockedSessionTest(unittest.TestCase):
    def test_locked_but_still_qualified_session_kept(self):
        corpus = load_corpus(ROOT / "data" / "session_two.json")
        pipeline = ReviewPipeline()
        first = pipeline.run(corpus)
        sheet = first.sheets[0]
        # 同一目的再跑一场：已送达锁定且评委仍合格的盲审单不需要换
        second = pipeline.run(corpus)
        self.assertTrue(sheet.delivered)
        self.assertEqual(second.assignments[0].judge.name,
                         first.assignments[0].judge.name)


if __name__ == "__main__":
    unittest.main()
