"""测甲（业务失败测）：旧代拨付串进新代对账仍假绿。

事故复现（本机文件库种子）：
  1. 同笼同代：入笼冻结 -> 核验通过 -> 解笼拨付（旧代 g1，部分拨付）；
  2. 运营把笼代从旧代升到新代（g2）；
  3. 新代对账却把旧代 g1 拨付吃进新代 g2 汇总 -> 互证假绿（balanced=True 假象）。

甲路径正确性口径：
  对账读写口强制带当前代令牌，跨代键（旧代拨付/罚没）一律不得计入新代合计；
  新代对账 released/forfeited 必须只统计当前代，旧代拨付留在旧代只读可查。
本测在修复前必须见红（released 被污染、假平衡或 404 升代口缺失）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture()
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seed_cross_generation(c: TestClient, h: dict) -> dict:
    """造「旧拨付串进新代」的事故种子，返回笼与各代金额坐标。"""
    c.post(
        "/api/bonds",
        headers=h,
        json={
            "party_key": "P-GEN-A",
            "party_name": "串代甲测试",
            "initial_deposit": 100000,
        },
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-GEN-A",
            "contract_no": "HT-GEN-A-1",
            "amount": 40000,
            "beneficiary": "受益甲",
            "cage_code": "CG-GEN-A-001",
        },
    ).json()
    cage_id = cage["id"]
    # 旧代 g1 核验通过并部分拨付 15000
    assert c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage_id, "outcome": "通过", "evidence": "g1 验收"},
    ).status_code == 200
    rel_g1 = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage_id, "amount": 15000, "payee": "受益甲"},
    )
    assert rel_g1.status_code == 200, rel_g1.text
    assert rel_g1.json()["cage_generation"] == 1
    # 运营升代：g1 -> g2（同笼升代，不重开笼、不动账）
    up = c.post("/api/cages/generation/upgrade", headers=h, json={"cage_id": cage_id})
    assert up.status_code == 200, f"升代口缺失或被拒: {up.status_code} {up.text}"
    assert up.json()["generation"] == 2
    return {"cage_id": cage_id, "cage_code": "CG-GEN-A-001", "old_release": 15000.0}


def test_new_generation_reconcile_excludes_old_generation_release(_db):
    c = TestClient(app)
    h = _auth(c)
    seed = _seed_cross_generation(c, h)
    cid, code = seed["cage_id"], seed["cage_code"]

    recon = c.get(f"/api/reconcile/{code}", headers=h)
    assert recon.status_code == 200, recon.text
    r = recon.json()
    # 甲路径：新代 g2 汇总不得吃进旧代 g1 拨付
    assert r["generation"] == 2
    assert r["released"] == 0.0, (
        f"串代：新代对账把旧代拨付 {seed['old_release']} 吃进合计，released={r['released']}"
    )
    assert r["forfeited"] == 0.0
    # 新代账面：可用 25000 + 当前代拨付 0 + 当前代罚没 0 == 新代代际基线 25000
    assert r["available"] == 25000.0
    assert r["balanced"] is True
    assert abs(r["gap"]) < 1e-6


def test_old_generation_release_still_readonly_queryable(_db):
    """甲路径：跨代键拒收于写口/汇总，但旧代拨付作为只读历史仍可查。"""
    c = TestClient(app)
    h = _auth(c)
    seed = _seed_cross_generation(c, h)
    rels = c.get("/api/releases", headers=h).json()["items"]
    old = [x for x in rels if x["cage_id"] == seed["cage_id"] and x["cage_generation"] == 1]
    assert len(old) == 1
    assert old[0]["amount"] == seed["old_release"]
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=? AND cage_generation=1",
            (seed["cage_id"],),
        ).fetchone()["c"]
        assert n == 1


def test_reconcile_all_summary_not_false_green(_db):
    """全量对账汇总：升代后该笼在新代口径下不得因旧代拨付而假绿/假红。"""
    c = TestClient(app)
    h = _auth(c)
    seed = _seed_cross_generation(c, h)
    rows = c.get("/api/reconcile", headers=h).json()["items"]
    row = next(x for x in rows if x["cage_id"] == seed["cage_id"])
    assert row["generation"] == 2
    assert row["released"] == 0.0
    assert row["balanced"] is True


def test_incident_repro_old_release_pollutes_new_generation_reconcile(_db):
    """事故直复（不依赖升代接口）：直接模拟运营把笼代升到 g2，
    当前缺陷代码的对账不带代过滤，旧代 g1 拨付被新代 g2 汇总吃进 -> 串代假绿。"""
    c = TestClient(app)
    h = _auth(c)
    c.post(
        "/api/bonds",
        headers=h,
        json={"party_key": "P-REPRO-A", "party_name": "事故复现甲", "initial_deposit": 100000},
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-REPRO-A",
            "contract_no": "HT-REPRO-A",
            "amount": 40000,
            "beneficiary": "受益",
            "cage_code": "CG-REPRO-A-001",
        },
    ).json()
    cid = cage["id"]
    c.post("/api/attestations", headers=h,
           json={"cage_id": cid, "outcome": "通过", "evidence": "g1"})
    assert c.post("/api/releases", headers=h,
                  json={"cage_id": cid, "amount": 15000}).status_code == 200
    # 运营升代（旧操作面：直接落库把代际翻到 2）
    with connect() as conn:
        conn.execute("UPDATE cages SET generation=2 WHERE id=?", (cid,))
        conn.commit()

    r = c.get("/api/reconcile/CG-REPRO-A-001", headers=h).json()
    assert r["generation"] == 2
    # 事故现象：缺陷代码把旧代 15000 拨付计入新代 released。
    # 正确口径（甲路径）：新代汇总 released 必须为 0，旧代拨付不进新代合计。
    assert r["released"] == 0.0, f"串代事故复现：新代吃下旧代拨付 released={r['released']}"
    assert r["balanced"] is True
