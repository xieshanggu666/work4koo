"""存量库迁移：应急资源与避难点协同调度。

在既有处置协同库基础上再做四件事（脚本幂等，可重复执行）：

1. 建立 shelters / emergency_resources / resource_assignments 三张新表
   （Base.metadata.create_all 自动补建，不动任何历史数据）；
2. evacuation_records 增加 arrived_people 已安置人数列
   （历史转移记录保持 0，原样展示）；
3. 避难点与应急资源主数据为空时注入演示储备（老库获得调度能力，
   已有数据则不覆盖）；
4. 建立 resource_assignments 幂等唯一索引
   (disposal_id, kind, target_id, zone_id)，数据库层兜住「同一处置单对
   同一目标至多一条调拨」；disposal_id 为 NULL 的历史遗留调拨不参与
   唯一约束、原样保留。

用法：python scripts/migrate_resource.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import Base, engine, SessionLocal

# 新表通过模型元数据补建（表已存在时 create_all 自动跳过）
from app import models  # noqa: E402,F401
from app.models import EmergencyResource, Shelter  # noqa: E402

UNIQUE_INDEXES = [
    ("uq_assignment_disposal_target", "resource_assignments",
     "disposal_id, kind, target_id, zone_id"),
]

# 老库补建的演示主数据（与 scripts/init_db.py 保持一致）
SEED_SHELTERS = [
    Shelter(id=1, name="白水高中避难点", capacity=20000, used=0, x=470, y=390),
    Shelter(id=2, name="龙潭中心小学避难点", capacity=12000, used=0, x=650, y=470),
    Shelter(id=3, name="古窑安置点", capacity=8000, used=0, x=460, y=600),
]
SEED_RESOURCES = [
    EmergencyResource(id=1, kind="vehicle", name="转移大巴", unit="辆",
                      total=60, available=60),
    EmergencyResource(id=2, kind="vehicle", name="冲锋舟", unit="艘",
                      total=25, available=25),
    EmergencyResource(id=3, kind="material", name="救生衣", unit="件",
                      total=30000, available=30000),
    EmergencyResource(id=4, kind="material", name="帐篷", unit="顶",
                      total=5000, available=5000),
    EmergencyResource(id=5, kind="material", name="应急食品包", unit="箱",
                      total=20000, available=20000),
]


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _add_column_if_missing(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        print(f"  + {table}.{column} 已添加")
    else:
        print(f"  = {table}.{column} 已存在，跳过")


def main():
    # 1. 补建新表（不影响存量数据）
    Base.metadata.create_all(bind=engine)
    print("[1/4] 避难点/应急资源/资源调拨表已就绪")

    with engine.begin() as conn:
        print("[2/4] 补充转移进度回写列（历史记录保持 0，原样保留）...")
        _add_column_if_missing(conn, "evacuation_records", "arrived_people",
                               "arrived_people INTEGER DEFAULT 0")

    # 3. 主数据为空时注入演示储备（幂等：已有数据不覆盖）
    db = SessionLocal()
    try:
        if db.query(Shelter).count() == 0:
            db.add_all(SEED_SHELTERS)
            print("[3/4] 已注入避难点演示数据（3 处）")
        else:
            print("[3/4] 避难点已有数据，跳过注入")
        if db.query(EmergencyResource).count() == 0:
            db.add_all(SEED_RESOURCES)
            print("       已注入应急资源演示数据（5 类）")
        else:
            print("       应急资源已有数据，跳过注入")
        db.commit()
    finally:
        db.close()

    with engine.begin() as conn:
        print("[4/4] 建立唯一索引（幂等键由数据库兜底）...")
        for name, table, cols in UNIQUE_INDEXES:
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
            print(f"  + {name} ON {table}({cols})")

    print("迁移完成：应急资源与避难点协同调度已启用（历史处置记录保持原样）。")


if __name__ == "__main__":
    main()
