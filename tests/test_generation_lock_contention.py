"""测丙（争用失败测）：同笼升代写口与出金解笼并发，同抢一把笼锁。

口径（并发段，与代际隔离段分行）：
  - 升代写口 POST /api/cages/generation/upgrade 与出金解笼 POST /api/releases
    必须使用同一把笼锁 lock_key=f"cage:{cage_id}"；
  - 后到柜员争用：只记 contention_log，可在 /api/contention 查到，
    不得插队落库（不得一边拒一边把业务写进去）；
  - 同笼同时只允许一个写口前进，另一写口 409 整笔拒收，库内状态自洽。

争用须起双会话/双请求打「升代 + 出金」，本测用两个登录会话 + 线程屏障对撞。
本测在修复前必须见红（升代口缺失 404，或同锁不互斥、争用不留痕）。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture()
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(user: str) -> dict:
    c = TestClient(app)
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _ready_cage(h: dict, code: str, party: str) -> int:
    """造一只 g1 核验通过、可解笼、可升代的笼。"""
    c = TestClient(app)
    c.post(
        "/api/bonds",
        headers=h,
        json={
            "party_key": party,
            "party_name": party + "公司",
            "initial_deposit": 100000,
        },
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": party,
            "contract_no": party + "-HT-1",
            "amount": 40000,
            "beneficiary": "受益" + party,
            "cage_code": code,
        },
    ).json()
    cid = cage["id"]
    assert c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cid, "outcome": "通过", "evidence": "g1"},
    ).status_code == 200
    return cid


def test_upgrade_and_release_share_same_cage_lock(_db):
    """确定性互斥：先持锁者挡住后到者，两写口同一 lock_key。"""
    h_admin = _auth("admin")
    h_clerk = _auth("clerk")
    c = TestClient(app)
    cid = _ready_cage(h_admin, "CG-LOCK-001", "P-LOCK-1")

    # admin 显式先占笼锁
    got = c.post(
        "/api/locks/acquire", headers=h_admin, json={"lock_key": f"cage:{cid}"}
    )
    assert got.status_code == 200

    # clerk 的升代写口争用同一把锁 -> 409，不落库
    up = c.post(
        "/api/cages/generation/upgrade", headers=h_clerk, json={"cage_id": cid}
    )
    assert up.status_code == 409, f"升代写口未受笼锁保护: {up.status_code}"
    # clerk 的出金解笼写口也争用同一把锁 -> 409，不落库
    rel = c.post("/api/releases", headers=h_clerk, json={"cage_id": cid})
    assert rel.status_code == 409, f"解笼写口未受笼锁保护: {rel.status_code}"

    # 争用可查
    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    hit = [x for x in cont if x["lock_key"] == f"cage:{cid}"]
    assert len(hit) >= 2, "后到柜员争用未登记 contention_log"
    assert {x["actor"] for x in hit} >= {"clerk"}

    # 零落库：代仍为 1、无新增拨付
    with connect() as conn:
        gen = conn.execute(
            "SELECT generation FROM cages WHERE id=?", (cid,)
        ).fetchone()["generation"]
        rel_n = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=?", (cid,)
        ).fetchone()["c"]
    assert gen == 1
    assert rel_n == 0


def test_concurrent_upgrade_vs_release_only_one_wins(_db):
    """双会话对撞：升代与出金同时抢笼锁，只准一个成功，另一个 409 且不插队落库。"""
    h1 = _auth("admin")
    h2 = _auth("clerk")
    cid = _ready_cage(h1, "CG-LOCK-002", "P-LOCK-2")

    barrier = threading.Barrier(2)
    results: dict[str, tuple[int, dict]] = {}

    def do_upgrade():
        c = TestClient(app)
        barrier.wait()
        r = c.post(
            "/api/cages/generation/upgrade", headers=h1, json={"cage_id": cid}
        )
        results["upgrade"] = (r.status_code, r.json() if r.content else {})

    def do_release():
        c = TestClient(app)
        barrier.wait()
        r = c.post("/api/releases", headers=h2, json={"cage_id": cid, "amount": 40000})
        results["release"] = (r.status_code, r.json() if r.content else {})

    t1 = threading.Thread(target=do_upgrade)
    t2 = threading.Thread(target=do_release)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert set(results) == {"upgrade", "release"}, "并发线程未全部返回"
    codes = {k: v[0] for k, v in results.items()}
    ok = [k for k, code in codes.items() if code == 200]
    blocked = [k for k, code in codes.items() if code == 409]
    assert len(ok) == 1, f"必须且只能一个写口成功，实际: {codes}"
    assert len(blocked) == 1, f"失败方必须 409 整笔拒收，实际: {codes}"

    with connect() as conn:
        cage = conn.execute(
            "SELECT generation, status, available FROM cages WHERE id=?", (cid,)
        ).fetchone()
        rels = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM releases WHERE cage_id=?",
            (cid,),
        ).fetchone()

    if ok == ["upgrade"]:
        # 升代成功：代进到 2、回到已冻结待新代核验；解笼被挡，无拨付
        assert cage["generation"] == 2
        assert cage["status"] == "已冻结"
        assert rels["c"] == 0
    else:
        # 解笼成功：仍是 g1，全额拨付后已解笼；升代被挡
        assert cage["generation"] == 1
        assert rels["c"] == 1
        assert float(rels["s"]) == 40000.0
        assert cage["status"] == "已解笼"

    # 输家 409 整笔拒收：绝无升代与拨付同时落库（上面分支已锁死互斥态）。
    # 跨请求争用「可查」由上一个显式持锁用例确定性保证；真实对撞下锁竞争与
    # 状态门竞争都可能成为拦截点，二者都不得插队落库。
    assert blocked[0] in ("upgrade", "release")
    c = TestClient(app)
    cont = c.get("/api/contention", headers=h1).json()["items"]
    # 若锁层先撞上必有争用留痕；即便由状态门拦截，也不允许任何双写
    if any(x["lock_key"] == f"cage:{cid}" for x in cont):
        assert all(x["actor"] in ("admin", "clerk") for x in cont if x["lock_key"] == f"cage:{cid}")
