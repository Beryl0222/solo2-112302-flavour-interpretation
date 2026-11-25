"""来源批次、分封改号与跨产区对照资格。

规则落点：
- 同一小批分封改了盲号仍是同一个来源（按批号聚合，不按盲号聚合）。
- 两次转记的相同意见不能被当成两个独立来源的分歧（去重在 disputes 里调用本模块）。
- 同来源留样不能当跨产区对照；只有登记在案、产区不同且用途相符的批次可作对照。
- 未登记杯位（来源不明）不能充当对照，转疑点处理。
"""
from __future__ import annotations

from typing import Optional

from review_flow.models import (
    ControlType,
    PackagingEvent,
    PromoExposure,
    SourceLot,
)


class UnknownCupError(LookupError):
    """杯位未在分样记录中登记。"""


class ProvenanceRegistry:
    def __init__(self) -> None:
        self._lots: dict[str, SourceLot] = {}
        self._cup_index: dict[str, str] = {}
        self._events: dict[str, PackagingEvent] = {}
        self._exposures: list[PromoExposure] = []

    # ---- 登记 ----------------------------------------------------------
    def add_lot(self, lot: SourceLot) -> None:
        if lot.lot_code in self._lots:
            raise ValueError(f"批号重复登记: {lot.lot_code}")
        self._lots[lot.lot_code] = lot
        for cup in lot.blind_cups:
            self._index_cup(cup, lot.lot_code)

    def add_event(self, event: PackagingEvent) -> None:
        if event.event_id in self._events:
            raise ValueError(f"分封事件重复: {event.event_id}")
        if event.lot_code not in self._lots:
            raise KeyError(f"分封事件指向未登记批号: {event.lot_code}")
        self._events[event.event_id] = event
        self._lots[event.lot_code].packaging_events.append(event)
        for cup in event.blind_cups:
            if cup not in self._cup_index:
                self._index_cup(cup, event.lot_code)
                self._lots[event.lot_code].blind_cups.append(cup)

    def _index_cup(self, cup: str, lot_code: str) -> None:
        owner = self._cup_index.get(cup)
        if owner is not None and owner != lot_code:
            raise ValueError(f"盲号 {cup} 已属于批号 {owner}，不能再挂到 {lot_code}")
        self._cup_index[cup] = lot_code

    def add_exposure(self, exposure: PromoExposure) -> None:
        self._exposures.append(exposure)

    # ---- 查询 ----------------------------------------------------------
    @property
    def lots(self) -> tuple[SourceLot, ...]:
        return tuple(self._lots.values())

    def lot_of_cup(self, cup: str) -> Optional[SourceLot]:
        lot_code = self._cup_index.get(cup)
        return self._lots.get(lot_code) if lot_code else None

    def require_lot_of_cup(self, cup: str) -> SourceLot:
        lot = self.lot_of_cup(cup)
        if lot is None:
            raise UnknownCupError(cup)
        return lot

    def is_same_source(self, cup_a: str, cup_b: str) -> bool:
        lot_a = self._cup_index.get(cup_a)
        lot_b = self._cup_index.get(cup_b)
        return bool(lot_a and lot_b and lot_a == lot_b)

    def is_cross_origin(self, cup_a: str, cup_b: str) -> bool:
        lot_a = self.lot_of_cup(cup_a)
        lot_b = self.lot_of_cup(cup_b)
        if lot_a is None or lot_b is None:
            return False
        return lot_a.region != lot_b.region

    def events_for_lot(self, lot_code: str) -> tuple[PackagingEvent, ...]:
        return tuple(self._lots[lot_code].packaging_events)

    def re_numbered_cups(self, lot_code: str) -> tuple[str, ...]:
        """该批号下因分封改号产生的全部盲号。"""
        cups: list[str] = []
        for event in self._lots[lot_code].packaging_events:
            cups.extend(event.blind_cups)
        return tuple(cups)

    # ---- 对照资格 ------------------------------------------------------
    def eligible_controls(self, disputed_lot_code: str,
                          control_type: ControlType) -> tuple[list[SourceLot], list[tuple[str, str]]]:
        """返回 (合格对照批次, 被剔除的(杯位或批号, 原因))。"""
        disputed = self._lots[disputed_lot_code]
        accepted: list[SourceLot] = []
        rejected: list[tuple[str, str]] = []
        for lot in self._lots.values():
            if lot.lot_code == disputed_lot_code:
                for cup in lot.blind_cups:
                    rejected.append((cup, "同来源留样不得充当跨产区对照"))
                continue
            if lot.region == disputed.region:
                rejected.append((lot.lot_code, "同产区，不满足跨产区对照"))
                continue
            if lot.control_type != control_type:
                rejected.append(
                    (lot.lot_code, f"对照用途不符：需要 {control_type.value}，登记为 "
                                   f"{lot.control_type.value if lot.control_type else '未登记'}")
                )
                continue
            accepted.append(lot)
        return accepted, rejected

    # ---- 利益冲突 ------------------------------------------------------
    def exposure_for(self, judge: str, lot_code: str) -> Optional[PromoExposure]:
        for exposure in self._exposures:
            if exposure.judge == judge and exposure.lot_code == lot_code:
                return exposure
        return None

    def exposed_lots(self, judge: str) -> tuple[str, ...]:
        return tuple(e.lot_code for e in self._exposures if e.judge == judge)
