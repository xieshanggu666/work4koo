"""应急资源与避难点协同调度测试。

覆盖：
- 规划 → 调拨 → 到位全流程与角色权限（越权 403、跳态 409）
- 到位回写：转移进度（arrived_people/status）与风险预警（安置到位解除）
- 容量/库存校验（超分 409）、调拨扣减库存、到位落账避难点容量
- 规划幂等（同一处置单×目标重复规划只更新数量）
- 历史兼容：历史处置单可直接补规划；run_id/disposal_id 为 NULL 的
  历史遗留台账不受回写影响
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, EmergencyResource, EvacuationRecord,
                        FloodZone, RainfallEvent, Reservoir, ResourceAssignment,
                        RiverNode, RiverReach, Shelter, SubBasin, WaterStation,
                        WarningRecord)
from app.services import disposal, resource
from app.services.forecast import run_forecast


def _seed(db):
    """自洽流域（强降雨必触发预警与强制转移）+ 避难点与应急资源储备。"""
    db.add_all([
        RiverNode(id=1, name="源头", kind="headwater"),
        RiverNode(id=2, name="库址", kind="reservoir"),
        RiverNode(id=3, name="出口", kind="outlet"),
        RiverReach(id=1, name="源→库", from_node_id=1, to_node_id=2,
                   k_hr=1.0, x_coef=0.2),
        RiverReach(id=2, name="库→出口", from_node_id=2, to_node_id=3,
                   k_hr=1.0, x_coef=0.2),
        SubBasin(id=1, name="子流域", area_km2=120.0, cn=88.0, lag_hr=1.0,
                 outlet_node_id=1),
        Reservoir(id=1, name="测试水库", node_id=2, normal_level=12.0,
                  flood_level=13.0, crest_level=16.0,
                  storage_curve=[[10, 100], [12, 300], [14, 600], [16, 1000], [18, 1500]],
                  discharge_curve=[[14, 0], [16, 200], [18, 600]],
                  gate_max=120.0, current_level=11.5, current_storage=300.0),
        WaterStation(id=1, name="出口水位站", node_id=3,
                     thresholds={"base_level": 5.0, "blue": 6.0, "yellow": 7.0,
                                 "orange": 8.0, "red": 9.0,
                                 "rating": [[0, 5.0], [30, 7.0], [60, 9.0], [100, 11.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=3, population=500,
                  low_level=6.0, high_level=8.0),
        # 第二风险区：阈值极高不触发转移台账，仅用于容量校验
        FloodZone(id=2, name="高地村", node_id=3, population=100,
                  low_level=999.0, high_level=999.0),
        RainfallEvent(id=1, name="测试暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
        # 避难点：两处合计容量 600 ≥ 转移人口 500
        Shelter(id=1, name="学校避难点", capacity=300, used=0),
        Shelter(id=2, name="体育馆避难点", capacity=300, used=0),
        # 应急资源：车辆 + 物资
        EmergencyResource(id=1, kind="vehicle", name="转移大巴", unit="辆",
                          total=10, available=10),
        EmergencyResource(id=2, kind="material", name="救生衣", unit="件",
                          total=1000, available=1000),
    ])
    db.commit()


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    # 与生产 SessionLocal 一致：autoflush=False，避免测试掩盖未刷新的查询
    fac = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = fac()
    _seed(db)
    db.close()
    yield fac
    engine.dispose()


def _reviewed_order(fac):
    """跑一次预报并把处置单推进到「待执行」（台账已挂接）。"""
    db = fac()
    r = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    o = disposal.initiate_order(db, r["run_id"], "张调度", "dispatcher")
    o = disposal.review_order(db, o["id"], "李值守", "duty")
    oid = o["id"]
    db.close()
    return oid


def _full_resource_loop(fac, oid):
    """规划 → 调拨 → 到位：避难容量 300+200、大巴 5 辆、救生衣 500 件。"""
    db = fac()
    resource.plan_assignment(db, oid, "shelter", 1, 1, 300, "王转移", "transfer_lead")
    resource.plan_assignment(db, oid, "shelter", 2, 1, 200, "王转移", "transfer_lead")
    resource.plan_assignment(db, oid, "vehicle", 1, 0, 5, "赵物资", "material_manager")
    resource.plan_assignment(db, oid, "material", 2, 0, 500, "赵物资", "material_manager")
    resource.dispatch_assignments(db, oid, "陈指挥", "commander")
    result = resource.confirm_arrival(db, oid, "陈指挥", "commander")
    db.close()
    return result


def test_full_resource_collaboration_loop(factory):
    oid = _reviewed_order(factory)
    result = _full_resource_loop(factory, oid)

    assert result["summary"]["arrived"] == 4
    assert result["summary"]["shelter_people"] == 500
    assert result["summary"]["vehicles"] == 5
    assert result["summary"]["materials"] == 500
    assert all(a["status"] == "arrived" for a in result["items"])
    assert all(a["planned_by"] and a["dispatched_by"] == "陈指挥"
               and a["arrived_by"] == "陈指挥" for a in result["items"])

    db = factory()
    # 库存扣减：大巴 10→5，救生衣 1000→500
    assert db.get(EmergencyResource, 1).available == 5
    assert db.get(EmergencyResource, 2).available == 500
    # 避难点容量落账
    assert db.get(Shelter, 1).used == 300
    assert db.get(Shelter, 2).used == 200
    # 回写 1：转移进度 —— 500 人全部安置，状态 safe
    evac = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id == oid).one()
    assert evac.arrived_people == 500 and evac.status == "safe"
    # 回写 2：风险预警 —— 安置到位，关联预警解除
    warn = db.query(WarningRecord).filter(WarningRecord.disposal_id == oid).one()
    assert warn.status == "cleared" and "安置到位" in warn.message
    # 处置记录追加调拨与到位小结
    order = db.get(DisposalOrder, oid)
    assert "[资源调拨]" in order.remark and "[资源到位]" in order.remark
    db.close()


def test_role_enforcement(factory):
    oid = _reviewed_order(factory)
    db = factory()
    # 物资管理员不能分配避难容量；转移负责人不能分配车辆/物资
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 1, 1, 100, "赵物资", "material_manager")
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "vehicle", 1, 0, 2, "王转移", "transfer_lead")
    assert ei.value.status_code == 403
    # 非指挥员不能下达调拨令 / 确认到位
    resource.plan_assignment(db, oid, "vehicle", 1, 0, 2, "赵物资", "material_manager")
    with pytest.raises(HTTPException) as ei:
        resource.dispatch_assignments(db, oid, "王转移", "transfer_lead")
    assert ei.value.status_code == 403
    resource.dispatch_assignments(db, oid, "陈指挥", "commander")
    with pytest.raises(HTTPException) as ei:
        resource.confirm_arrival(db, oid, "赵物资", "material_manager")
    assert ei.value.status_code == 403
    db.close()


def test_state_machine_rejects_illegal_actions(factory):
    oid = _reviewed_order(factory)
    db = factory()
    # 无规划直接调拨
    with pytest.raises(HTTPException) as ei:
        resource.dispatch_assignments(db, oid, "陈指挥", "commander")
    assert ei.value.status_code == 409
    # 未调拨直接确认到位
    resource.plan_assignment(db, oid, "vehicle", 1, 0, 2, "赵物资", "material_manager")
    with pytest.raises(HTTPException) as ei:
        resource.confirm_arrival(db, oid, "陈指挥", "commander")
    assert ei.value.status_code == 409
    # 处置单闭环后不能再规划/调拨
    disposal.execute_order(db, oid, "王转移", "transfer_lead")
    disposal.complete_order(db, oid, "王转移", "transfer_lead")
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "vehicle", 1, 0, 1, "赵物资", "material_manager")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        resource.dispatch_assignments(db, oid, "陈指挥", "commander")
    assert ei.value.status_code == 409
    db.close()


def test_capacity_and_stock_validation(factory):
    oid = _reviewed_order(factory)
    db = factory()
    # 超出避难点容量（300）→ 409
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 1, 1, 301, "王转移", "transfer_lead")
    assert ei.value.status_code == 409
    # 规划 200 后，同一避难点剩余容量只剩 100（跨风险区累计校验）
    resource.plan_assignment(db, oid, "shelter", 1, 1, 200, "王转移", "transfer_lead")
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 1, 2, 101, "王转移", "transfer_lead")
    assert ei.value.status_code == 409
    # 幂等更新同一目标时同样校验容量（300 容量不能改为 301）
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 1, 1, 301, "王转移", "transfer_lead")
    assert ei.value.status_code == 409
    # 超出资源可用量（大巴 10 辆）→ 409
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "vehicle", 1, 0, 11, "赵物资", "material_manager")
    assert ei.value.status_code == 409
    # 数量必须为正
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "vehicle", 1, 0, 0, "赵物资", "material_manager")
    assert ei.value.status_code == 400
    db.close()


def test_repeat_plan_is_idempotent_and_locked_after_dispatch(factory):
    oid = _reviewed_order(factory)
    db = factory()
    a1 = resource.plan_assignment(db, oid, "shelter", 1, 1, 100, "王转移", "transfer_lead")
    a2 = resource.plan_assignment(db, oid, "shelter", 1, 1, 150, "王转移", "transfer_lead")
    assert a1["id"] == a2["id"] and a2["quantity"] == 150
    assert db.query(ResourceAssignment).count() == 1
    # 调拨后同一目标不能再改数量
    resource.dispatch_assignments(db, oid, "陈指挥", "commander")
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 1, 1, 200, "王转移", "transfer_lead")
    assert ei.value.status_code == 409
    db.close()


def test_partial_arrival_moves_progress_without_clearing_warning(factory):
    oid = _reviewed_order(factory)
    db = factory()
    # 只安置 500 人中的 200 人
    resource.plan_assignment(db, oid, "shelter", 1, 1, 200, "王转移", "transfer_lead")
    resource.dispatch_assignments(db, oid, "陈指挥", "commander")
    resource.confirm_arrival(db, oid, "陈指挥", "commander")

    evac = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id == oid).one()
    assert evac.arrived_people == 200 and evac.status == "moving"
    warn = db.query(WarningRecord).filter(WarningRecord.disposal_id == oid).one()
    assert warn.status == "active"  # 未全部安置，预警保持生效
    db.close()


def test_legacy_disposal_order_can_be_scheduled(factory):
    """历史处置单（本功能上线前已存在）可直接补做资源调度并回写其台账。"""
    db = factory()
    r = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    # 模拟历史处置单：直接落库、无资源调拨记录；其台账历史上已挂接
    legacy = DisposalOrder(run_id=r["run_id"], title="历史处置单", status="approved",
                         initiated_by="老调度")
    db.add(legacy)
    db.commit()
    oid = legacy.id
    (db.query(EvacuationRecord).filter(EvacuationRecord.run_id == r["run_id"])
     .update({"disposal_id": oid}))
    (db.query(WarningRecord).filter(WarningRecord.run_id == r["run_id"])
     .update({"disposal_id": oid}))
    db.commit()
    db.close()

    result = _full_resource_loop(factory, oid)
    assert result["summary"]["arrived"] == 4
    db = factory()
    evac = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id == oid).one()
    assert evac.arrived_people == 500 and evac.status == "safe"
    warn = db.query(WarningRecord).filter(WarningRecord.disposal_id == oid).one()
    assert warn.status == "cleared"
    db.close()


def test_legacy_null_disposal_records_untouched_by_writeback(factory):
    """历史遗留台账（disposal_id 为 NULL）不受资源到位回写影响。"""
    oid = _reviewed_order(factory)
    db = factory()
    db.add(WarningRecord(run_id=None, disposal_id=None, target_type="station",
                         target_id=99, target_name="历史站", kind="water_level",
                         level="blue", status="active"))
    db.add(EvacuationRecord(run_id=None, disposal_id=None, zone_id=99,
                            zone_name="历史村", triggered_by="预警提示",
                            people=120, status="moving"))
    db.commit()
    db.close()

    _full_resource_loop(factory, oid)

    db = factory()
    legacy_w = db.query(WarningRecord).filter(WarningRecord.disposal_id.is_(None)).one()
    legacy_e = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id.is_(None)).one()
    assert legacy_w.status == "active"
    assert legacy_e.status == "moving" and legacy_e.arrived_people == 0
    db.close()


def test_overview_reflects_reservations(factory):
    oid = _reviewed_order(factory)
    db = factory()
    resource.plan_assignment(db, oid, "shelter", 1, 1, 200, "王转移", "transfer_lead")
    resource.plan_assignment(db, oid, "vehicle", 1, 0, 3, "赵物资", "material_manager")
    ov = resource.resource_overview(db)
    s1 = next(s for s in ov["shelters"] if s["id"] == 1)
    assert s1["reserved"] == 200 and s1["remaining"] == 100
    bus = next(r for r in ov["resources"] if r["id"] == 1)
    assert bus["reserved"] == 3 and bus["plannable"] == 7 and bus["available"] == 10
    db.close()


def test_unknown_order_or_target_404(factory):
    db = factory()
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, 999, "shelter", 1, 1, 100, "王转移", "transfer_lead")
    assert ei.value.status_code == 404
    oid = _reviewed_order(factory)
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 999, 1, 100, "王转移", "transfer_lead")
    assert ei.value.status_code == 404
    # 避难容量必须指定风险区
    with pytest.raises(HTTPException) as ei:
        resource.plan_assignment(db, oid, "shelter", 1, 0, 100, "王转移", "transfer_lead")
    assert ei.value.status_code == 404
    db.close()
