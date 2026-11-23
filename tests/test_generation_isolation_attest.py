"""测乙（业务失败测）：解笼还能挂旧代凭证号核验通过（串代核验）。

事故复现：
  1. 旧代 g1 核验通过（attest_gen=1），部分拨付后升代到 g2；
  2. 新代 g2 未做任何核验；
  3. 解笼服务只挑「任意一条 outcome=通过」的核验，不校验 attest_gen 是否=当前代，
     于是新代解笼竟挂旧代 g1 凭证号通过核验、成功落库 —— 互证看似齐实则串代。

甲路径正确性口径：
  写口（解笼）强制带当前代令牌；核验凭证必须 attest_gen == cages.generation，
  遇旧代引用一律正式零改拒写（409），不新增 releases、不改笼额/账户、不留晋升痕。
本测在修复前必须见红（解笼 200 成功串代落库）。
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


def _upgrade_with_stale_g1_attest(c: TestClient, h: dict) -> int:
    c.post(
        "/api/bonds",
        headers=h,
        json={
            "party_key": "P-GEN-B",
            "party_name": "串代乙测试",
            "initial_deposit": 100000,
        },
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-GEN-B",
            "contract_no": "HT-GEN-B-1",
            "amount": 40000,
            "beneficiary": "受益乙",
            "cage_code": "CG-GEN-B-001",
        },
    ).json()
    cage_id = cage["id"]
    # 旧代 g1 核验通过 + 部分拨付，留 25000 可用
    assert c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage_id, "outcome": "通过", "evidence": "g1 旧凭证"},
    ).status_code == 200
    assert c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage_id, "amount": 15000},
    ).status_code == 200
    up = c.post(
        "/api/cages/generation/upgrade", headers=h, json={"cage_id": cage_id}
    )
    assert up.status_code == 200, f"升代口缺失: {up.status_code} {up.text}"
    return cage_id


def test_release_with_stale_generation_attest_is_rejected(_db):
    c = TestClient(app)
    h = _auth(c)
    cage_id = _upgrade_with_stale_g1_attest(c, h)

    # 升代后笼回到「已冻结」待新代核验，当前代 g2 无任何核验
    cage = c.get(f"/api/cages/{cage_id}", headers=h).json()["cage"]
    assert cage["generation"] == 2
    assert cage["status"] == "已冻结"

    with connect() as conn:
        before_rel = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        before_avail = conn.execute(
            "SELECT available FROM cages WHERE id=?", (cage_id,)
        ).fetchone()["available"]

    # 试图在新代 g2 解笼 —— 只能挂到旧代 g1 凭证，必须正式零改拒写
    r = c.post("/api/releases", headers=h, json={"cage_id": cage_id, "amount": 1000})
    assert r.status_code == 409, f"串代：旧代凭证竟让新代解笼通过 status={r.status_code}"

    with connect() as conn:
        after_rel = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        after_avail = conn.execute(
            "SELECT available FROM cages WHERE id=?", (cage_id,)
        ).fetchone()["available"]
        after_status = conn.execute(
            "SELECT status FROM cages WHERE id=?", (cage_id,)
        ).fetchone()["status"]
    # 零增：无新拨付；零改：笼可用额/状态未动
    assert after_rel == before_rel, "拒写后竟新增了拨付行"
    assert float(after_avail) == float(before_avail) == 25000.0
    assert after_status == "已冻结"


def test_release_succeeds_only_after_current_generation_attest(_db):
    """正面口径：新代 g2 重新核验通过后，解笼才允许，且凭证对齐 g2。"""
    c = TestClient(app)
    h = _auth(c)
    cage_id = _upgrade_with_stale_g1_attest(c, h)

    # 新代 g2 核验通过
    att = c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage_id, "outcome": "通过", "evidence": "g2 新凭证"},
    )
    assert att.status_code == 200
    assert att.json()["attest_gen"] == 2

    r = c.post("/api/releases", headers=h, json={"cage_id": cage_id, "amount": 5000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cage_generation"] == 2
    # 该笔新代拨付只计入新代，对账不串
    recon = c.get("/api/reconcile/CG-GEN-B-001", headers=h).json()
    assert recon["generation"] == 2
    assert recon["released"] == 5000.0


def test_attest_write_against_old_generation_reference_rejected(_db):
    """核验写口本身：同笼代唯一结论 + 升代后旧代结论不得被当作当前代有效结论。"""
    c = TestClient(app)
    h = _auth(c)
    cage_id = _upgrade_with_stale_g1_attest(c, h)
    # g2 尚未核验时，解笼前置态必须是「已冻结」而非沿用 g1 的「核验通过」
    detail = c.get(f"/api/cages/{cage_id}", headers=h).json()
    atts = detail["attestations"]
    g1_pass = [a for a in atts if a["attest_gen"] == 1 and a["outcome"] == "通过"]
    g2_pass = [a for a in atts if a["attest_gen"] == 2 and a["outcome"] == "通过"]
    assert len(g1_pass) == 1  # 旧代凭证只读留痕
    assert len(g2_pass) == 0  # 新代无凭证，不得冒充


def test_incident_repro_stale_attest_approves_new_generation_release(_db):
    """事故直复（不依赖升代接口）：直接模拟运营升代并把状态留在「核验通过」，
    缺陷解笼服务只挑任意一条 outcome=通过 的核验而不看代际，
    于是新代 g2 解笼竟挂旧代 g1 凭证号核验通过、成功落库。"""
    c = TestClient(app)
    h = _auth(c)
    c.post(
        "/api/bonds",
        headers=h,
        json={"party_key": "P-REPRO-B", "party_name": "事故复现乙", "initial_deposit": 100000},
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-REPRO-B",
            "contract_no": "HT-REPRO-B",
            "amount": 40000,
            "beneficiary": "受益",
            "cage_code": "CG-REPRO-B-001",
        },
    ).json()
    cid = cage["id"]
    c.post("/api/attestations", headers=h,
           json={"cage_id": cid, "outcome": "通过", "evidence": "g1 旧凭证"})
    c.post("/api/releases", headers=h, json={"cage_id": cid, "amount": 15000})
    # 运营升代：代际翻到 2，但状态被旧结论带在「核验通过」（缺陷面）
    with connect() as conn:
        conn.execute(
            "UPDATE cages SET generation=2, status='核验通过' WHERE id=?", (cid,)
        )
        conn.commit()

    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]

    # 新代 g2 无本代核验，解笼必须被正式零改拒写；缺陷代码却放行
    r = c.post("/api/releases", headers=h, json={"cage_id": cid, "amount": 1000})
    assert r.status_code == 409, (
        f"串代事故复现：新代解笼挂旧代凭证竟通过 status={r.status_code}"
    )
    with connect() as conn:
        after = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
    assert after == before, "事故复现：串代解笼零增约束被破坏（不应落库）"
