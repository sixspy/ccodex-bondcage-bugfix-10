"""对账互证：当前代「可用 + 当前代已拨付 + 当前代已罚没」须等于当前代代际基线。

甲路径 · 代际隔离段：读写口强制带当前代令牌（取库内 cages.generation），
releases/forfeits 一律按 cage_generation = 当前代 过滤；旧代拨付/罚没作为只读
历史保留，绝不并入新代合计，从根上杜绝「旧拨付串进新代对账仍假绿」。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _generation_spent(conn, cage_id: int, current_gen: int) -> tuple[float, float]:
    """返回 (旧代已拨付, 旧代已罚没)：仅用于推算当前代代际基线，不进当前代合计。"""
    if current_gen <= 1:
        return 0.0, 0.0
    old_rel = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM releases
           WHERE cage_id=? AND status='已拨付' AND cage_generation<?""",
        (cage_id, current_gen),
    ).fetchone()["s"]
    old_for = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM forfeits
           WHERE cage_id=? AND status='已罚没' AND cage_generation<?""",
        (cage_id, current_gen),
    ).fetchone()["s"]
    return float(old_rel), float(old_for)


def _reconcile_cage(conn, cage) -> dict:
    cage_id = cage["id"]
    gen = int(cage["generation"])
    amount = float(cage["amount"])
    available = float(cage["available"])
    # 甲路径：当前代令牌带库，跨代键（旧代拨付/罚没）一律不并入当前代合计。
    released = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM releases
           WHERE cage_id=? AND status='已拨付' AND cage_generation=?""",
        (cage_id, gen),
    ).fetchone()["s"]
    forfeited = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM forfeits
           WHERE cage_id=? AND status='已罚没' AND cage_generation=?""",
        (cage_id, gen),
    ).fetchone()["s"]
    # 当前代代际基线 = 笼原始总额 − 旧代已结算（旧代拨付+旧代罚没）。
    # 同笼升代零额变动、available 跨代承接，故以基线作为当前代恒等式右端。
    old_released, old_forfeited = _generation_spent(conn, cage_id, gen)
    gen_baseline = round(amount - old_released - old_forfeited, 6)
    accounted = round(float(available) + float(released) + float(forfeited), 6)
    ok = abs(accounted - gen_baseline) < 1e-6
    # 账户侧：该当事人冻结额应覆盖所有仍冻结/核验中的笼可用
    party = cage["party_key"]
    acct = conn.execute(
        "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
        (party,),
    ).fetchone()
    live_frozen = conn.execute(
        """SELECT COALESCE(SUM(available),0) s FROM cages
           WHERE party_key=? AND status IN ('已冻结','核验通过','核验违约','部分解笼','部分罚没')""",
        (party,),
    ).fetchone()["s"]
    return {
        "cage_id": cage_id,
        "cage_code": cage["cage_code"],
        "party_key": party,
        "contract_no": cage["contract_no"],
        "generation": gen,
        "status": cage["status"],
        "amount": amount,
        "available": available,
        "released": float(released),
        "forfeited": float(forfeited),
        "old_generation_released": old_released,
        "old_generation_forfeited": old_forfeited,
        "generation_baseline": gen_baseline,
        "accounted": accounted,
        "balanced": ok,
        "gap": round(gen_baseline - accounted, 6),
        "account_balance": float(acct["balance"]) if acct else None,
        "account_frozen": float(acct["frozen"]) if acct else None,
        "live_cage_available": float(live_frozen),
        "proof": {
            "formula": "available + 当前代released + 当前代forfeited == 当前代代际基线（笼总额−旧代已结算）",
            "left": accounted,
            "right": gen_baseline,
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cages = conn.execute("SELECT * FROM cages ORDER BY id").fetchall()
        items = [_reconcile_cage(conn, c) for c in cages]
        bad = [i for i in items if not i["balanced"]]
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
            },
        }


@router.get("/api/reconcile/{cage_code}")
def reconcile_one(cage_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute(
            "SELECT * FROM cages WHERE cage_code=?", (cage_code,)
        ).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        return _reconcile_cage(conn, cage)


@router.get("/api/proof/{party_key}")
def party_proof(party_key: str, authorization: AuthHeader = None):
    """当事人级互证：账户冻结 vs 活笼可用合计。"""
    require_user(authorization)
    with connect() as conn:
        acct = conn.execute(
            "SELECT * FROM bond_accounts WHERE party_key=?", (party_key,)
        ).fetchone()
        if not acct:
            raise HTTPException(404, "保证金户不存在")
        live = conn.execute(
            """SELECT COALESCE(SUM(available),0) s FROM cages
               WHERE party_key=? AND status IN
               ('已冻结','核验通过','核验违约','部分解笼','部分罚没')""",
            (party_key,),
        ).fetchone()["s"]
        released = conn.execute(
            """SELECT COALESCE(SUM(r.amount),0) s FROM releases r
               JOIN cages c ON c.id=r.cage_id
               WHERE c.party_key=? AND r.status='已拨付'""",
            (party_key,),
        ).fetchone()["s"]
        forfeited = conn.execute(
            """SELECT COALESCE(SUM(f.amount),0) s FROM forfeits f
               JOIN cages c ON c.id=f.cage_id
               WHERE c.party_key=? AND f.status='已罚没'""",
            (party_key,),
        ).fetchone()["s"]
        frozen = float(acct["frozen"])
        live_f = float(live)
        return {
            "party_key": party_key,
            "account": dict(acct),
            "live_cage_available": live_f,
            "released_total": float(released),
            "forfeited_total": float(forfeited),
            "frozen_matches_live": abs(frozen - live_f) < 1e-6,
            "proof": {
                "formula": "account.frozen == sum(live cage available)",
                "left": frozen,
                "right": live_f,
            },
        }
