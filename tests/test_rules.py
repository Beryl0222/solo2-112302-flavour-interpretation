"""领域规则专项：非独立分歧、不足退回、术语重指定底层行为。

不依赖语料文本适配层，直接构造领域对象，保证规则本身可单独复用。
"""
import unittest

from review_flow.disputes import (
    CLASSIFICATION_DUPLICATE,
    DisputeDetector,
)
from review_flow.models import (
    Assertion,
    AssertionKind,
    AssignmentStatus,
    ControlType,
    Judge,
    PackagingEvent,
    Polarity,
    ReviewPurpose,
    SenseType,
    SourceLot,
    TermSense,
)
from review_flow.notes import NoteParser, split_sentences
from review_flow.provenance import ProvenanceRegistry
from review_flow.review import ReviewPlanner
from review_flow.terminology import Glossary


def build_glossary() -> Glossary:
    glossary = Glossary()
    glossary.add_sense(TermSense(
        code="fermentation", canonical="发酵大类",
        sense_type=SenseType.UMBRELLA, languages={"zh": ("发酵",)},
    ))
    glossary.add_sense(TermSense(
        code="winey_style", canonical="酒香风格", sense_type=SenseType.SENSE,
        umbrella_codes=("fermentation",),
        languages={"en": ("winey",), "zh": ("酒香",)},
        conditional="仅在洁净杯语境适用",
    ))
    glossary.add_sense(TermSense(
        code="ferment_defect", canonical="发酵缺陷", sense_type=SenseType.SENSE,
        umbrella_codes=("fermentation",),
        languages={"en": ("ferment defect",), "zh": ("发酵缺陷",)},
    ))
    return glossary


def build_registry() -> ProvenanceRegistry:
    registry = ProvenanceRegistry()
    lot = SourceLot(lot_code="L1", name="甲地一号", region="甲地",
                    blind_cups=["C1", "C2"], record_id="x1")
    registry.add_lot(lot)
    registry.add_event(PackagingEvent(
        event_id="E1", lot_code="L1", blind_cups=("C1", "C2"),
        record_id="x1", re_numbered_from=("C1",),
    ))
    # 跨产区缺陷对照
    registry.add_lot(SourceLot(
        lot_code="L9", name="乙地九号", region="乙地", blind_cups=["C9"],
        control_type=ControlType.KNOWN_DEFECT, record_id="x9",
    ))
    return registry


def assertion(record_id: str, judge: str, cup: str, sense_code: str,
              polarity: Polarity, pour_seq=None) -> Assertion:
    return Assertion(
        record_id=record_id, judge=judge, blind_cup=cup,
        sentence_index=0, span=(0, 1), sentence=f"{judge}-{cup}",
        kind=AssertionKind.ATTRIBUTE, polarity=polarity,
        term_text=sense_code, sense_code=sense_code,
        sense_type=SenseType.SENSE,
        umbrella_codes=("fermentation",), pour_seq=pour_seq,
    )


class NonIndependentDisputeTest(unittest.TestCase):
    def test_same_judge_same_pour_renumbered_conflict_is_duplicate(self):
        glossary = build_glossary()
        registry = build_registry()
        parser = NoteParser(glossary)
        notes = [
            parser.parse("a1", "评委辛", "C1", "Winey; no ferment defect."),
            parser.parse("a2", "评委辛", "C2", "明显的 ferment defect。"),
        ]
        # 让两条意见都由同一评委在同冲注、改号杯上给出
        for note in notes:
            for item in note.assertions:
                pass  # 解析结果保持自然极性
        detector = DisputeDetector(glossary, registry)
        result = detector.detect(notes)
        dispute = next(d for d in result.disputes if "ferment_defect" in d.sense_codes)
        self.assertFalse(dispute.independent)
        self.assertEqual(dispute.classification, CLASSIFICATION_DUPLICATE)

        planner = ReviewPlanner(registry, [
            Judge("评委壬", (ReviewPurpose.DEFECT_CONFIRMATION,)),
        ])
        assignment = planner.plan(dispute)
        self.assertEqual(assignment.status, AssignmentStatus.INSUFFICIENT)
        self.assertIn("不构成独立分歧", assignment.reason)

    def test_different_pours_remain_independent(self):
        glossary = build_glossary()
        registry = build_registry()
        parser = NoteParser(glossary)
        first = parser.parse("b1", "评委辛", "C1", "Winey.", pour_seq=1)
        second = parser.parse("b2", "评委辛", "C1", "ferment defect", pour_seq=2)
        detector = DisputeDetector(glossary, registry)
        result = detector.detect([first, second])
        dispute = result.disputes[0]
        self.assertTrue(dispute.independent)


class InsufficientReturnTest(unittest.TestCase):
    def test_no_qualified_judge_returns_insufficient(self):
        glossary = build_glossary()
        registry = build_registry()
        parser = NoteParser(glossary)
        notes = [
            parser.parse("c1", "评委辛", "C1", "Winey."),
            parser.parse("c2", "评委壬", "C2", "ferment defect."),
        ]
        detector = DisputeDetector(glossary, registry)
        dispute = detector.detect(notes).disputes[0]
        planner = ReviewPlanner(registry, [
            Judge("只懂香型", (ReviewPurpose.CALIBRATION,)),
        ])
        assignment = planner.plan(dispute)
        self.assertEqual(assignment.status, AssignmentStatus.INSUFFICIENT)
        self.assertIsNone(assignment.judge)

    def test_no_control_returns_insufficient(self):
        glossary = build_glossary()
        registry = ProvenanceRegistry()
        registry.add_lot(SourceLot(
            lot_code="L1", name="甲地一号", region="甲地",
            blind_cups=["C1", "C2"], record_id="x1",
        ))
        parser = NoteParser(glossary)
        notes = [
            parser.parse("d1", "评委辛", "C1", "Winey."),
            parser.parse("d2", "评委壬", "C2", "ferment defect."),
        ]
        dispute = DisputeDetector(glossary, registry).detect(notes).disputes[0]
        planner = ReviewPlanner(registry, [
            Judge("全能", (ReviewPurpose.CALIBRATION, ReviewPurpose.DEFECT_CONFIRMATION)),
        ])
        assignment = planner.plan(dispute)
        self.assertEqual(assignment.status, AssignmentStatus.INSUFFICIENT)
        self.assertIn("对照", assignment.reason)


class GlossaryRedesignationTest(unittest.TestCase):
    def test_redesignation_moves_surface_and_keeps_history(self):
        glossary = build_glossary()
        new_table = glossary.redesignate("发酵", "ferment_defect", "v2")
        self.assertEqual(new_table.revision, "v2")
        result = new_table.resolve("发酵")
        self.assertEqual(result.sense_code, "ferment_defect")
        self.assertEqual(result.sense_type, SenseType.SENSE)
        # 原表不被修改
        self.assertEqual(glossary.resolve("发酵").sense_type, SenseType.UMBRELLA)
        self.assertIn(("v2", "发酵", "ferment_defect"), new_table.history)
        with self.assertRaises(ValueError):
            new_table.redesignate("酒香", "fermentation", "v3")  # 不能指回大类


class SentenceLocatorTest(unittest.TestCase):
    def test_split_offsets_and_spans(self):
        text = "Winey in the aroma, no ferment defect; the reference Y04 was dull."
        sentences = split_sentences(text)
        self.assertEqual(len(sentences), 2)
        offset, first = sentences[0]
        self.assertEqual(text[offset:offset + len(first)], first)
        glossary = build_glossary()
        parsed = NoteParser(glossary).parse("z1", "评委", "C1", text)
        located = [a for a in parsed.assertions if a.sense_code == "ferment_defect"]
        self.assertTrue(located)
        for item in located:
            self.assertEqual(
                item.sentence[item.span[0]:item.span[1]], "ferment defect",
            )


if __name__ == "__main__":
    unittest.main()
