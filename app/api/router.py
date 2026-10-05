from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import (DisposalOrder, EvacuationRecord, FloodZone, ForecastRun,
                        ForecastSeries, RainStation, RainfallEvent, Reservoir,
                        RiverNode, RiverReach, SubBasin, WaterStation, WarningRecord)
from app.services import disposal as disposal_svc
from app.services.forecast import run_forecast

router = APIRouter(prefix="/api")


# ---------------- 联合防汛处置协同请求体 ----------------
class InitiateBody(BaseModel):
    operator: str = ""          # 操作人姓名
    role: str = "dispatcher"    # dispatcher 调度员
    title: str = ""
    remark: str = ""


class ReviewBody(BaseModel):
    operator: str = ""
    role: str = "duty"          # duty 预警值守
    opinion: str = ""           # 审核意见


class ExecuteBody(BaseModel):
    operator: str = ""
    role: str = "transfer_lead"  # transfer_lead 转移负责人
    note: str = ""


class CompleteBody(BaseModel):
    operator: str = ""
    role: str = "transfer_lead"
    summary: str = ""


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    res = db.query(Reservoir).all()
    stations = db.query(WaterStation).all()
    events = db.query(RainfallEvent).all()
    subs = db.query(SubBasin).all()
    zones = db.query(FloodZone).all()
    return {
        "basin_name": "青岚江流域",
        "sub_basins": len(subs),
        "reaches": db.query(RiverReach).count(),
        "rain_stations": db.query(RainStation).count(),
        "water_stations": len(stations),
        "reservoirs": len(res),
        "events": len(events),
        "zones": len(zones),
        "reservoir_ready": sum(1 for r in res if r.current_level),
        "total_capacity": round(sum(r.storage_at(r.crest_level) for r in res), 1),
        "population_at_risk": sum(z.population for z in zones),
    }


@router.get("/map")
def basin_map(db: Session = Depends(get_db)):
    nodes = [{"id": n.id, "name": n.name, "kind": n.kind, "x": n.x, "y": n.y}
             for n in db.query(RiverNode).all()]
    reaches = [{"id": r.id, "name": r.name, "from": r.from_node_id, "to": r.to_node_id,
                "length": r.length_km}
               for r in db.query(RiverReach).all()]
    subs = [{"id": s.id, "name": s.name, "area_km2": s.area_km2, "cn": s.cn,
             "lag_hr": s.lag_hr, "outlet_node_id": s.outlet_node_id, "x": s.x, "y": s.y}
            for s in db.query(SubBasin).all()]
    reservoirs = [{"id": r.id, "name": r.name, "node_id": r.node_id,
                   "normal_level": r.normal_level, "flood_level": r.flood_level,
                   "crest_level": r.crest_level, "current_level": r.current_level,
                   "current_storage": r.current_storage,
                   "gate_max": r.gate_max, "x": r.x, "y": r.y}
                  for r in db.query(Reservoir).all()]
    stations = [{"id": s.id, "name": s.name, "node_id": s.node_id, "x": s.x, "y": s.y,
                 "thresholds": s.thresholds}
                for s in db.query(WaterStation).all()]
    rain_stations = [{"id": s.id, "name": s.name, "node_id": s.node_id, "x": s.x, "y": s.y}
                     for s in db.query(RainStation).all()]
    zones = [{"id": z.id, "name": z.name, "node_id": z.node_id, "risk_level": z.risk_level,
              "population": z.population, "low_level": z.low_level, "high_level": z.high_level,
              "route": z.route, "x": z.x, "y": z.y} for z in db.query(FloodZone).all()]
    return {"nodes": nodes, "reaches": reaches, "sub_basins": subs,
            "reservoirs": reservoirs, "stations": stations,
            "rain_stations": rain_stations, "flood_zones": zones}


@router.get("/rain-events")
def rainfall_events(db: Session = Depends(get_db)):
    return [{"id": e.id, "name": e.name, "return_period": e.return_period,
             "duration_h": e.duration_h, "total_mm": e.total_mm, "note": e.note}
            for e in db.query(RainfallEvent).all()]


@router.get("/rain-events/{eid}")
def rain_event_detail(eid: int, db: Session = Depends(get_db)):
    e = db.query(RainfallEvent).get(eid)
    if not e:
        return {"detail": "not found"}
    return {"id": e.id, "name": e.name, "return_period": e.return_period,
            "duration_h": e.duration_h, "total_mm": e.total_mm,
            "hyetograph": e.hyetograph, "note": e.note}


@router.get("/reservoirs")
def reservoirs(db: Session = Depends(get_db)):
    return [{"id": r.id, "name": r.name, "node_id": r.node_id,
             "normal_level": r.normal_level, "flood_level": r.flood_level,
             "crest_level": r.crest_level, "current_level": r.current_level,
             "current_storage": r.current_storage, "gate_max": r.gate_max}
            for r in db.query(Reservoir).all()]


@router.get("/warnings")
def warnings(db: Session = Depends(get_db)):
    return [{"id": w.id, "run_id": w.run_id, "disposal_id": w.disposal_id,
             "target_type": w.target_type,
             "target_id": w.target_id,
             "target_name": w.target_name, "level": w.level, "value": w.value,
             "threshold": w.threshold, "message": w.message,
             "created_at": w.created_at.isoformat() if w.created_at else None,
             "status": w.status}
            for w in db.query(WarningRecord).order_by(WarningRecord.id.desc()).all()]


@router.get("/evacuations")
def evacuations(db: Session = Depends(get_db)):
    return [{"id": e.id, "run_id": e.run_id, "disposal_id": e.disposal_id,
             "zone_id": e.zone_id, "zone_name": e.zone_name,
             "triggered_by": e.triggered_by, "people": e.people, "status": e.status,
             "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in db.query(EvacuationRecord).order_by(EvacuationRecord.id.desc()).all()]


@router.post("/forecast/{eid}/{mode}")
def forecast(eid: int, mode: str, db: Session = Depends(get_db)):
    event = db.query(RainfallEvent).get(eid)
    if not event:
        return {"detail": "event not found"}
    return run_forecast(db, event, reservoir_rule=mode)


@router.get("/forecast/runs")
def forecast_runs(db: Session = Depends(get_db)):
    """历史预报记录，附带处置单状态（无处置单时 disposal 为 null）。"""
    runs = (db.query(ForecastRun)
            .order_by(ForecastRun.id.desc()).limit(20).all())
    orders = {o.run_id: o for o in db.query(DisposalOrder).all()}
    result = []
    for r in runs:
        event = db.get(RainfallEvent, r.event_id)
        order = orders.get(r.id)
        result.append({
            "id": r.id, "event_id": r.event_id,
            "event_name": event.name if event else "（情景已删除）",
            "mode": r.mode, "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "disposal": ({"id": order.id, "status": order.status}
                         if order else None),
        })
    return result


@router.get("/forecast/series/{run_id}")
def forecast_series(run_id: int, db: Session = Depends(get_db)):
    rows = db.query(ForecastSeries).filter(ForecastSeries.run_id == run_id).all()
    return [{"id": r.id, "node_id": r.node_id, "kind": r.kind, "name": r.name,
             "values": r.values} for r in rows]


# ---------------- 联合防汛处置协同 ----------------
@router.get("/disposals")
def disposal_list(db: Session = Depends(get_db)):
    return disposal_svc.list_orders(db)


@router.get("/disposals/{order_id}")
def disposal_detail(order_id: int, db: Session = Depends(get_db)):
    return disposal_svc.get_order(db, order_id)


@router.post("/disposals/from-run/{run_id}")
def disposal_initiate(run_id: int, body: InitiateBody, db: Session = Depends(get_db)):
    """调度员围绕预报运行发起处置单（同一运行重复发起幂等返回已存在单据）。"""
    return disposal_svc.initiate_order(db, run_id, body.operator, body.role,
                                       body.title, body.remark)


@router.post("/disposals/{order_id}/review")
def disposal_review(order_id: int, body: ReviewBody, db: Session = Depends(get_db)):
    """预警值守审核通过：回写水库工况、预警与转移台账。"""
    return disposal_svc.review_order(db, order_id, body.operator, body.role, body.opinion)


@router.post("/disposals/{order_id}/execute")
def disposal_execute(order_id: int, body: ExecuteBody, db: Session = Depends(get_db)):
    """转移负责人启动执行：转移台账进入转移中。"""
    return disposal_svc.execute_order(db, order_id, body.operator, body.role, body.note)


@router.post("/disposals/{order_id}/complete")
def disposal_complete(order_id: int, body: CompleteBody, db: Session = Depends(get_db)):
    """转移负责人确认完成：转移到位、预警销警，处置闭环。"""
    return disposal_svc.complete_order(db, order_id, body.operator, body.role, body.summary)
