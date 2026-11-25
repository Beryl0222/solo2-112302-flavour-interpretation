"""领域数据结构。

所有结构都是普通 dataclass，字段名用英文，注释面向秘书工作流。
设计原则：每条被解析出来的意见都能回溯到原句序号、字符跨度和杯位，
盲号、来源批次、分封事件分别建模，避免把同来源改号杯当成两个来源。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Polarity(Enum):
    """意见极性：肯定该属性，还是否定该属性（短评里被否定的属性）。"""

    AFFIRMED = "affirmed"
    NEGATED = "negated"


class AssertionKind(Enum):
    """句子的证据类型。"""

    ATTRIBUTE = "attribute"        # 直接描述某属性（含否定）
    COMPARISON = "comparison"      # 与对照杯比较
    HEAD_QUOTE = "head_quote"      # 描述人引用主审用词


class SenseType(Enum):
    """词义类型：具体义项 / 仅用于检索的统一大类 / 未登记词。"""

    SENSE = "sense"
    UMBRELLA = "umbrella"
    UNRESOLVED = "unresolved"


class ReviewPurpose(Enum):
    """送复评的目的，由争议的义项性质决定，可因重新指定词义而改变。"""

    CALIBRATION = "calibration"   # 校准香型
    DEFECT_CONFIRMATION = "defect_confirmation"  # 确认缺陷


class ControlType(Enum):
    """对照杯用途。"""

    CLEAN_STYLE = "clean_style"      # 洁净杯语境的风格训练杯
    KNOWN_DEFECT = "known_defect"    # 已确认缺陷对照杯
    SAME_ORIGIN_RETAIN = "same_origin_retain"  # 同来源留样（禁止当跨产区对照）


class AssignmentStatus(Enum):
    """派单状态。"""

    ASSIGNED = "assigned"        # 已派给合格评委
    RECUSED = "recused"          # 因利益冲突改派
    INSUFFICIENT = "insufficient"  # 合格人或对照不足，退回
    LOCKED = "locked"            # 已送达锁定，仍合格，不换


@dataclass(frozen=True)
class TermSense:
    """一个词义义项或检索大类。

    umbrella=True 表示“统一大类”，只方便检索；
    它不能被用来把两个具体义项上的意见直接抵消。
    conditional 记录条件适用词及其语境约束（例如 winey 仅在洁净杯语境）。
    """

    code: str                                  # 义项编号，如 winey_style
    canonical: str                             # 规范含义
    sense_type: SenseType
    umbrella_codes: tuple[str, ...] = ()       # 所属统一大类编号
    languages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    conditional: Optional[str] = None          # 条件适用说明
    glossary_record_id: Optional[str] = None   # 出处记录
    revision: str = "v1"

    def surface_forms(self) -> tuple[str, ...]:
        forms: list[str] = []
        for vals in self.languages.values():
            forms.extend(vals)
        return tuple(forms)


@dataclass(frozen=True)
class PackagingEvent:
    """一次分封/转记事件。

    同一小批分封后改了盲号仍是同一个来源：事件指向来源批号，
    并登记本次事件产生或转记的盲号。
    """

    event_id: str
    lot_code: str
    blind_cups: tuple[str, ...]
    record_id: str
    note: str = ""
    re_numbered_from: tuple[str, ...] = ()  # 本次由哪些旧盲号改号而来


@dataclass
class SourceLot:
    """来源批次（小批）。"""

    lot_code: str
    name: str
    region: str
    blind_cups: list[str] = field(default_factory=list)
    packaging_events: list[PackagingEvent] = field(default_factory=list)
    control_type: Optional[ControlType] = None
    record_id: Optional[str] = None


@dataclass(frozen=True)
class PromoExposure:
    """评委接触过某来源批次宣传稿的记录（利益冲突线索）。"""

    judge: str
    lot_code: str
    record_id: str
    detail: str = ""


@dataclass(frozen=True)
class Assertion:
    """短评中的一条可定位意见。

    定位信息：杯位 blind_cup、句子序号 sentence_index、字符跨度 span、
    原句 sentence 与整条记录 record_id。
    term_text 是原语言片段；sense_code 解析到具体义项，
    umbrella 命中统一大类，unresolved 则登记疑点等待查原语言片段。
    """

    record_id: str
    judge: str
    blind_cup: str
    sentence_index: int
    span: tuple[int, int]
    sentence: str
    kind: AssertionKind
    polarity: Polarity
    term_text: str
    sense_code: Optional[str] = None
    sense_type: SenseType = SenseType.UNRESOLVED
    umbrella_codes: tuple[str, ...] = ()
    compared_cup: Optional[str] = None      # 对照比较里的对照杯位
    quote_attribution: Optional[str] = None  # 引用的是谁的用词（如主审）
    uncertainty: bool = False               # 描述人自己未确认
    pour_seq: Optional[int] = None          # 第几次冲注


@dataclass
class ParsedNote:
    """一条杯测短评解析结果，保留原文。"""

    record_id: str
    judge: str
    blind_cup: str
    raw_text: str
    assertions: list[Assertion] = field(default_factory=list)
    pour_seq: Optional[int] = None


@dataclass
class OpinionGroup:
    """按（杯位来源、义项、极性、证据类型）归并的一组意见。"""

    key: str
    lot_code: str
    sense_code: str
    sense_type: SenseType
    polarity: Polarity
    kind: AssertionKind
    assertions: list[Assertion] = field(default_factory=list)
    merged_duplicates: list[Assertion] = field(default_factory=list)

    @property
    def judges(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(a.judge for a in self.assertions))


@dataclass
class Doubt:
    """复评前必须保留的疑点（不猜测、不强行归类）。"""

    doubt_id: str
    reason: str
    record_ids: tuple[str, ...]
    blind_cups: tuple[str, ...] = ()
    raw_fragment: Optional[str] = None  # 允许秘书查原语言片段
    detail: str = ""


@dataclass
class Dispute:
    """认定的真实分歧（或经去重后不构成独立分歧的同案）。"""

    dispute_id: str
    lot_code: str
    blind_cups: tuple[str, ...]
    sense_codes: tuple[str, ...]
    umbrella_codes: tuple[str, ...]
    groups: list[OpinionGroup] = field(default_factory=list)
    classification: str = ""           # 见 disputes.CLASSIFICATION_*
    recommended_purpose: ReviewPurpose = ReviewPurpose.CALIBRATION
    independent: bool = True
    merged_note: str = ""
    evidence_assertions: list[Assertion] = field(default_factory=list)
    doubts: list[Doubt] = field(default_factory=list)
    excluded_controls: list[str] = field(default_factory=list)  # 同来源留样杯位


@dataclass
class Judge:
    """复评席评委：可承担的复评目的与宣传稿接触史。"""

    name: str
    qualified_purposes: tuple[ReviewPurpose, ...]
    exposures: list[PromoExposure] = field(default_factory=list)
    record_id: Optional[str] = None
    holds_sheets: tuple[str, ...] = ()  # 已领取的送审单编号


@dataclass
class ControlRequirement:
    """某复评目的所要求的对照杯类型与说明。"""

    purpose: ReviewPurpose
    required_types: tuple[ControlType, ...]
    rationale: str


@dataclass
class ReviewAssignment:
    """一次派单结论。"""

    dispute_id: str
    purpose: ReviewPurpose
    status: AssignmentStatus
    judge: Optional[Judge] = None
    controls: list[SourceLot] = field(default_factory=list)
    rejected_judges: list[tuple[str, str]] = field(default_factory=list)  # (评委, 原因)
    rejected_controls: list[tuple[str, str]] = field(default_factory=list)
    doubts: list[Doubt] = field(default_factory=list)
    reason: str = ""


@dataclass
class SheetEntry:
    """盲审复评单上的一行：只含盲号与脱敏材料。"""

    entry_no: int
    blind_cup: str
    role: str                 # 待复评 / 对照（不写来源与产区）
    redacted_excerpt: str     # 脱敏后的原句片段
    doubts: tuple[str, ...] = ()


@dataclass
class ReviewSheet:
    """交给盲审组的复评单：按适格材料组合，不含身份信息。"""

    sheet_id: str
    assignment_id: str
    purpose: ReviewPurpose
    judge_name: str
    entries: list[SheetEntry] = field(default_factory=list)
    open_doubts: tuple[str, ...] = ()
    delivered: bool = False
    frozen_blind_cups: tuple[str, ...] = ()  # 送达后冻结的杯位集合


@dataclass(frozen=True)
class SealedKeyEntry:
    """封存密钥中的单行：盲号 -> 来源证据。"""

    blind_cup: str
    lot_code: str
    region: str
    evidence_record_ids: tuple[str, ...]
    packaging_event_ids: tuple[str, ...]


@dataclass
class SealedKey:
    """封存密钥：仅原产区负责人可开，盲审单本身推不出同案其他杯身份。"""

    key_id: str
    sheet_id: str
    region: str
    entries: list[SealedKeyEntry] = field(default_factory=list)

    def entry_for(self, blind_cup: str) -> Optional[SealedKeyEntry]:
        for entry in self.entries:
            if entry.blind_cup == blind_cup:
                return entry
        return None


@dataclass
class DeliveryRecord:
    """盲审送达记录：送达后冻结盲号对应，后续说明不得暴露新对应关系。"""

    sheet_id: str
    judge_name: str
    purpose: ReviewPurpose
    frozen_cups: tuple[str, ...]
    locked: bool = True
