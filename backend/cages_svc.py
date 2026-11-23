"""入笼冻结：可用保证金划入笼内，写笼代。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import cage_write_tx, release, require_lock
from models import CageFreezeIn, GenerationUpgradeIn

router = APIRouter(tags=["cages"])

# 升代允许的活态；终态（已解笼/已罚没）与零可用笼不得升代
UPGRADABLE_STATUS = (
    "已冻结",
    "核验通过",
    "核验违约",
    "部分解笼",
    "部分罚没",
)


def _next_cage_code(conn, party_key: str) -> str:
    n = conn.execute(
        "SELECT COUNT(*) c FROM cages WHERE party_key=?", (party_key,)
    ).fetchone()["c"]
    return f"CG-{party_key}-{n + 1:03d}"


@router.get("/api/cages")
def list_cages(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cages WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cages ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/cages/{cage_id}")
def get_cage(cage_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        atts = conn.execute(
            "SELECT * FROM attestations WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        rels = conn.execute(
            "SELECT * FROM releases WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        fors = conn.execute(
            "SELECT * FROM forfeits WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        return {
            "cage": dict(cage),
            "attestations": [dict(a) for a in atts],
            "releases": [dict(r) for r in rels],
            "forfeits": [dict(f) for f in fors],
        }


@router.post("/api/cages/freeze")
def freeze_cage(body: CageFreezeIn, authorization: AuthHeader = None):
    """从账户可用余额划入新笼并冻结。"""
    user = require_writer(authorization)
    lock_key = f"bond:{body.party_key}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            acct = conn.execute(
                "SELECT * FROM bond_accounts WHERE party_key=?",
                (body.party_key,),
            ).fetchone()
            if not acct:
                raise HTTPException(404, "保证金户不存在")
            if acct["status"] != "正常":
                raise HTTPException(409, "账户状态禁止入笼")
            amount = float(body.amount)
            free = float(acct["balance"]) - float(acct["frozen"])
            if amount > free + 1e-9:
                raise HTTPException(409, "可用保证金不足，无法入笼")
            code = body.cage_code or _next_cage_code(conn, body.party_key)
            dup = conn.execute(
                "SELECT id FROM cages WHERE cage_code=?", (code,)
            ).fetchone()
            if dup:
                raise HTTPException(409, "笼编码已存在")
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO cages(cage_code,party_key,contract_no,amount,available,
                   generation,status,beneficiary,created_at,frozen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.party_key,
                    body.contract_no,
                    amount,
                    amount,
                    1,
                    "已冻结",
                    body.beneficiary,
                    now,
                    now,
                ),
            )
            cage_id = cur.lastrowid
            conn.execute(
                "UPDATE bond_accounts SET frozen = frozen + ? WHERE party_key=?",
                (amount, body.party_key),
            )
            write_audit(
                conn,
                user,
                "cage_freeze",
                code,
                {
                    "cage_id": cage_id,
                    "amount": amount,
                    "contract_no": body.contract_no,
                    "beneficiary": body.beneficiary,
                    "generation": 1,
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/cages/generation/upgrade")
def upgrade_generation(body: GenerationUpgradeIn, authorization: AuthHeader = None):
    """同笼升代写口（甲路径 · 代际隔离段）。

    - 当前代令牌只取库内 cages.generation，目标代 = 当前代 + 1，客户端无法指定/伪造；
    - 只翻代际与状态，零额变动：cages.amount/available 与 bond_accounts 一律不动；
    - 升代后笼回到「已冻结」，强制新代重新核验，旧代核验/拨付/罚没仅作只读历史；
    - 并发段：与解笼/核验/罚没共用同一把 cage:{id} 笼锁，后到争用 409 不插队落库。
    """
    user = require_writer(authorization)
    lock_key = f"cage:{body.cage_id}"
    with connect() as conn:
        # 并发段：与解笼/核验/罚没共用同一把 cage:{id} 笼锁；争用即留痕并 409。
        with cage_write_tx(conn, lock_key, user):
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")
            if cage["status"] not in UPGRADABLE_STATUS:
                raise HTTPException(409, f"笼状态 {cage['status']} 禁止升代")
            if float(cage["available"]) <= 0:
                raise HTTPException(409, "笼可用额为 0，禁止空笼升代")
            old_gen = int(cage["generation"])
            new_gen = old_gen + 1
            # 零额变动：只翻代际、状态回待核验；金额/可用/账户全部不碰
            conn.execute(
                "UPDATE cages SET generation=?, status='已冻结' WHERE id=?",
                (new_gen, body.cage_id),
            )
            write_audit(
                conn,
                user,
                "generation_upgrade",
                cage["cage_code"],
                {
                    "cage_id": body.cage_id,
                    "from_generation": old_gen,
                    "to_generation": new_gen,
                    "amount_unchanged": float(cage["amount"]),
                    "available_unchanged": float(cage["available"]),
                    "note": "same_cage_generation_upgrade_zero_balance_change",
                },
            )
        row = conn.execute(
            "SELECT * FROM cages WHERE id=?", (body.cage_id,)
        ).fetchone()
        return dict(row)
