"""应急资源与避难点协同调度：围绕预警处置单分配避难容量、车辆与物资。

角色与状态机
    转移负责人 plan(shelter)   → 规划避难点容量（按风险区）
    物资管理员 plan(vehicle/material) → 规划车辆与物资
    指挥员     dispatch        → 下达调拨令：planned → dispatched（扣减资源可用量）
    指挥员     arrive          → 确认到位：dispatched → arrived（避难点容量落账，
                                 回写转移进度 arrived_people/status，安置到位的
                                 风险区关联预警解除）

每条调拨按 (disposal_id, kind, target_id, zone_id) 幂等：重复规划只更新
数量；已调拨/已到位的调拨不可再改。历史处置单（无资源调拨记录）可直接
补规划；disposal_id 为 NULL 的历史遗留调拨原样保留，不参与唯一约束；
回写只触及挂接本处置单的转移/预警台账，历史遗留台账（disposal_id 为
NULL）不受影响。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (DisposalOrder, EmergencyResource, EvacuationRecord,
                        FloodZone, ResourceAssignment, Shelter, WarningRecord,
                        WaterStation)

KIND_TEXT = {"shelter": "避难容量", "vehicle": "车辆", "material": "物资"}
STATUS_TEXT = {"planned": "已规划", "dispatched": "已调拨", "arrived": "已到位"}
ROLE_TEXT = {"transfer_lead": "转移负责人", "material_manager": "物资管理员",
             "commander": "指挥员"}
# 各类调拨的规划角色；调拨令与到位确认均由指挥员执行
PLAN_ROLES = {"shelter": ("transfer_lead",), "vehicle": ("material_manager",),
              "material": ("material_manager",)}

_assignment_locks_guard = threading.Lock()
_assignment_locks: Dict[int, threading.Lock] = {}


def _disposal_lock(disposal_id: int) -> threading.Lock:
    """同一处置单的资源调度串行化，避免并发规划/调拨造成容量与库存错乱。"""
    with _assignment_locks_guard:
        return _assignment_locks.setdefault(disposal_id, threading.Lock())


def _get_order(db: Session, disposal_id: int) -> DisposalOrder:
    order = db.get(DisposalOrder, disposal_id)
    if order is None:
        raise HTTPException(404, f"处置单 #{disposal_id} 不存在")
    if order.status == "completed":
        raise HTTPException(409, f"处置单 #{disposal_id} 已闭环，资源调度已归档，不能再变更")
    return order


def _check_role(role: str, allowed: tuple, action: str) -> None:
    if role not in allowed:
        need = "、".join(ROLE_TEXT[r] for r in allowed)
        raise HTTPException(403, f"{action}需由{need}执行"
                                 f"（当前角色：{ROLE_TEXT.get(role, role)}）")


def _reserved_for_shelter(db: Session, shelter_id: int, exclude_id: int = 0) -> int:
    """避难点已被在途调拨占用的容量（planned/dispatched；arrived 已计入 used）。"""
    rows = (db.query(ResourceAssignment)
            .filter(ResourceAssignment.kind == "shelter",
                    ResourceAssignment.target_id == shelter_id,
                    ResourceAssignment.status.in_(("planned", "dispatched")))
            .all())
    return sum(r.quantity for r in rows if r.id != exclude_id)


def _reserved_for_resource(db: Session, resource_id: int, exclude_id: int = 0) -> int:
    """资源已被规划占用的数量（planned；dispatched 已在调拨时扣减 available）。"""
    rows = (db.query(ResourceAssignment)
            .filter(ResourceAssignment.kind.in_(("vehicle", "material")),
                    ResourceAssignment.target_id == resource_id,
                    ResourceAssignment.status == "planned")
            .all())
    return sum(r.quantity for r in rows if r.id != exclude_id)


def serialize_assignment(a: ResourceAssignment) -> dict:
    return {
        "id": a.id,
        "disposal_id": a.disposal_id,
        "kind": a.kind,
        "kind_text": KIND_TEXT.get(a.kind, a.kind),
        "target_id": a.target_id,
        "target_name": a.target_name,
        "zone_id": a.zone_id,
        "zone_name": a.zone_name,
        "quantity": a.quantity,
        "status": a.status,
        "status_text": STATUS_TEXT.get(a.status, a.status),
        "planned_by": a.planned_by,
        "dispatched_by": a.dispatched_by,
        "arrived_by": a.arrived_by,
        "planned_at": a.planned_at.isoformat() if a.planned_at else None,
        "dispatched_at": a.dispatched_at.isoformat() if a.dispatched_at else None,
        "arrived_at": a.arrived_at.isoformat() if a.arrived_at else None,
    }


def list_assignments(db: Session, disposal_id: int) -> dict:
    """处置单的资源调拨台账 + 汇总（含历史遗留调拨原样展示）。"""
    rows = (db.query(ResourceAssignment)
            .filter(ResourceAssignment.disposal_id == disposal_id)
            .order_by(ResourceAssignment.id).all())
    summary = {"planned": 0, "dispatched": 0, "arrived": 0,
               "shelter_people": 0, "vehicles": 0, "materials": 0}
    for a in rows:
        summary[a.status] = summary.get(a.status, 0) + 1
        if a.status == "arrived":
            if a.kind == "shelter":
                summary["shelter_people"] += a.quantity
            elif a.kind == "vehicle":
                summary["vehicles"] += a.quantity
            elif a.kind == "material":
                summary["materials"] += a.quantity
    return {"items": [serialize_assignment(a) for a in rows], "summary": summary}


def plan_assignment(db: Session, disposal_id: int, kind: str, target_id: int,
                    zone_id: int, quantity: int, operator: str, role: str) -> dict:
    """规划一条资源调拨（按处置单+目标幂等，重复规划只更新数量）。

    避难容量由转移负责人按风险区规划；车辆/物资由物资管理员规划。
    容量/库存校验把在途调拨一并计入，防止超分。
    """
    if kind not in KIND_TEXT:
        raise HTTPException(400, f"未知调拨类型：{kind}")
    _check_role(role, PLAN_ROLES[kind], f"规划{KIND_TEXT[kind]}")
    if quantity is None or quantity <= 0:
        raise HTTPException(400, "调拨数量必须为正整数")

    with _disposal_lock(disposal_id):
        _get_order(db, disposal_id)

        zone_name = ""
        if kind == "shelter":
            target = db.get(Shelter, target_id)
            if target is None:
                raise HTTPException(404, f"避难点 #{target_id} 不存在")
            if target.status == "closed":
                raise HTTPException(409, f"避难点「{target.name}」已关闭，不能规划安置")
            zone = db.get(FloodZone, zone_id) if zone_id else None
            if zone is None:
                raise HTTPException(404, "避难容量须指定安置的风险区")
            zone_name = zone.name
        else:
            target = db.get(EmergencyResource, target_id)
            if target is None or target.kind != kind:
                raise HTTPException(404, f"{KIND_TEXT[kind]}资源 #{target_id} 不存在")
            zone_id = 0  # 车辆/物资面向整个处置单，不绑定风险区

        # 幂等归并：同一处置单×类型×目标×风险区 至多一条调拨
        existing = (db.query(ResourceAssignment)
                    .filter(ResourceAssignment.disposal_id == disposal_id,
                            ResourceAssignment.kind == kind,
                            ResourceAssignment.target_id == target_id,
                            ResourceAssignment.zone_id == zone_id)
                    .first())
        if existing is not None and existing.status != "planned":
            raise HTTPException(409, f"该{KIND_TEXT[kind]}调拨{STATUS_TEXT[existing.status]}，"
                                     "不能再修改数量")
        exclude = existing.id if existing else 0

        if kind == "shelter":
            remaining = target.capacity - target.used - _reserved_for_shelter(
                db, target_id, exclude)
            if quantity > remaining:
                raise HTTPException(409, f"避难点「{target.name}」剩余容量不足："
                                         f"可规划 {remaining} 人，申请 {quantity} 人")
        else:
            remaining = target.available - _reserved_for_resource(db, target_id, exclude)
            if quantity > remaining:
                raise HTTPException(409, f"「{target.name}」可用量不足："
                                         f"可规划 {remaining}{target.unit}，申请 {quantity}{target.unit}")

        if existing is not None:
            existing.quantity = quantity
            existing.planned_by = operator.strip() or ROLE_TEXT[role]
            existing.planned_at = datetime.now()
            db.commit()
            db.refresh(existing)
            return serialize_assignment(existing)

        rec = ResourceAssignment(
            disposal_id=disposal_id, kind=kind, target_id=target_id,
            target_name=target.name, zone_id=zone_id, zone_name=zone_name,
            quantity=quantity, status="planned",
            planned_by=operator.strip() or ROLE_TEXT[role])
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return serialize_assignment(rec)


def dispatch_assignments(db: Session, disposal_id: int, operator: str,
                         role: str, note: str = "") -> dict:
    """指挥员下达调拨令：全部已规划调拨 → 已调拨，并扣减资源可用量。"""
    _check_role(role, ("commander",), "下达调拨令")
    with _disposal_lock(disposal_id):
        order = _get_order(db, disposal_id)
        rows = (db.query(ResourceAssignment)
                .filter(ResourceAssignment.disposal_id == disposal_id,
                        ResourceAssignment.status == "planned")
                .all())
        if not rows:
            raise HTTPException(409, "没有待调拨的规划，请先由转移负责人/物资管理员完成规划")

        now = datetime.now()
        by = operator.strip() or ROLE_TEXT["commander"]
        for a in rows:
            if a.kind in ("vehicle", "material"):
                res = db.get(EmergencyResource, a.target_id)
                if res is None or res.available < a.quantity:
                    raise HTTPException(409, f"「{a.target_name}」可用量不足，无法调拨")
                res.available -= a.quantity
            a.status = "dispatched"
            a.dispatched_by = by
            a.dispatched_at = now

        parts = [f"{KIND_TEXT[k]}{sum(a.quantity for a in rows if a.kind == k)}"
                 f"{'人' if k == 'shelter' else '项'}"
                 for k in ("shelter", "vehicle", "material")
                 if any(a.kind == k for a in rows)]
        order.remark = (order.remark + f"\n[资源调拨] {by}下达调拨令："
                        + "、".join(parts) + (f"；{note.strip()}" if note.strip() else "")).strip()
        db.commit()
        return list_assignments(db, disposal_id)


def confirm_arrival(db: Session, disposal_id: int, operator: str,
                    role: str, note: str = "") -> dict:
    """指挥员确认到位：已调拨 → 已到位，回写转移进度与风险预警。

    回写 1（转移进度）：按风险区汇总到位避难容量，更新转移台账
    arrived_people；全部安置置 safe，部分安置且未启动的置 moving。
    回写 2（风险预警）：安置到位的风险区，其关联预警（站点/风险区目标）
    统一解除（cleared）。只触及挂接本处置单的台账，历史遗留记录不受影响。
    """
    _check_role(role, ("commander",), "确认到位")
    with _disposal_lock(disposal_id):
        order = _get_order(db, disposal_id)
        rows = (db.query(ResourceAssignment)
                .filter(ResourceAssignment.disposal_id == disposal_id,
                        ResourceAssignment.status == "dispatched")
                .all())
        if not rows:
            raise HTTPException(409, "没有待确认到位的调拨（须先下达调拨令）")

        now = datetime.now()
        by = operator.strip() or ROLE_TEXT["commander"]
        for a in rows:
            if a.kind == "shelter":
                shelter = db.get(Shelter, a.target_id)
                if shelter is not None:
                    shelter.used = min(shelter.capacity, shelter.used + a.quantity)
                    if shelter.used >= shelter.capacity:
                        shelter.status = "full"
            a.status = "arrived"
            a.arrived_by = by
            a.arrived_at = now

        db.flush()  # 会话 autoflush=False：先落盘再在库内汇总到位调拨

        # ---- 回写 1：转移进度 ----
        arrived_all = (db.query(ResourceAssignment)
                       .filter(ResourceAssignment.disposal_id == disposal_id,
                               ResourceAssignment.kind == "shelter",
                               ResourceAssignment.status == "arrived")
                       .all())
        sheltered_by_zone: Dict[int, int] = {}
        for a in arrived_all:
            sheltered_by_zone[a.zone_id] = sheltered_by_zone.get(a.zone_id, 0) + a.quantity

        evacs = (db.query(EvacuationRecord)
                 .filter(EvacuationRecord.disposal_id == disposal_id).all())
        fully_sheltered_zones = []
        for ev in evacs:
            arrived = min(ev.people, sheltered_by_zone.get(ev.zone_id, 0))
            ev.arrived_people = arrived
            if ev.people > 0 and arrived >= ev.people:
                ev.status = "safe"
                fully_sheltered_zones.append(ev.zone_id)
            elif arrived > 0 and ev.status == "pending":
                ev.status = "moving"

        # ---- 回写 2：风险预警（安置到位的风险区解除关联预警）----
        if fully_sheltered_zones:
            zones = (db.query(FloodZone)
                     .filter(FloodZone.id.in_(fully_sheltered_zones)).all())
            node_ids = {z.node_id for z in zones}
            warnings = (db.query(WarningRecord)
                        .filter(WarningRecord.disposal_id == disposal_id,
                                WarningRecord.status == "active").all())
            station_by_node = {s.node_id: s.id for s in db.query(WaterStation).all()}
            for w in warnings:
                hit_zone = (w.target_type == "zone" and w.target_id in fully_sheltered_zones)
                hit_station = (w.target_type == "station"
                               and any(station_by_node.get(nid) == w.target_id
                                       for nid in node_ids))
                if hit_zone or hit_station:
                    w.status = "cleared"
                    w.message = (w.message + "（群众已安置到位，预警解除）")[:200]

        order.remark = (order.remark + f"\n[资源到位] {by}确认到位："
                        f"安置 {sum(sheltered_by_zone.values())} 人"
                        + (f"；{note.strip()}" if note.strip() else "")).strip()
        db.commit()
        return list_assignments(db, disposal_id)


def resource_overview(db: Session) -> dict:
    """避难点与应急资源总览（含在途占用，供调度面板展示）。"""
    shelters = []
    for s in db.query(Shelter).order_by(Shelter.id).all():
        reserved = _reserved_for_shelter(db, s.id)
        shelters.append({
            "id": s.id, "name": s.name, "capacity": s.capacity, "used": s.used,
            "reserved": reserved, "remaining": s.capacity - s.used - reserved,
            "status": s.status, "x": s.x, "y": s.y,
        })
    resources = []
    for r in db.query(EmergencyResource).order_by(EmergencyResource.id).all():
        reserved = _reserved_for_resource(db, r.id)
        resources.append({
            "id": r.id, "kind": r.kind, "kind_text": KIND_TEXT.get(r.kind, r.kind),
            "name": r.name, "unit": r.unit, "total": r.total,
            "available": r.available, "reserved": reserved,
            "plannable": r.available - reserved,
        })
    return {"shelters": shelters, "resources": resources}
