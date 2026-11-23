"""笼锁与争用登记（公共并发面）。

并发段口径：同笼一切写口（入笼/核验/解笼/罚没/升代/增补/拆笼）都抢同一把
lock_key=f"cage:{cage_id}"（账户级写口抢 bond:{party_key}）。后到柜员争用：
只登记 contention_log 可查，绝不插队落库；双会话真实对撞下若 SQLite 在插入
持锁行时直接抛唯一约束/库锁冲突，同样按「争用」处置而非 500。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM cage_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        # 双会话同时走到这里：唯一约束会拦下后到者，按争用处理而非冒泡 500。
        try:
            conn.execute(
                "INSERT INTO cage_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                "SELECT holder FROM cage_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
        except sqlite3.OperationalError as exc:  # database is locked 等库级争用
            conn.rollback()
            conn.execute(
                """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (lock_key, holder, "争用", f"库级锁冲突:{exc}", now),
            )
            return False
    if row is not None and row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder'] if row else '未知'}", now),
    )
    return False


@contextmanager
def cage_write_tx(conn, lock_key: str, holder: str):
    """同笼写口统一事务边界（并发段）。

    1. 先获锁：若争用，contention_log 立即提交留痕后抛 409（不被业务回滚带走）；
    2. 获锁成功后开 SAVEPOINT 跑业务：业务异常只回滚到保存点（零增零改），
       锁册动作与争用留痕独立于业务事务，绝不丢失；
    3. 退出时释放本持有者的笼锁。
    """
    try:
        require_lock(conn, lock_key, holder)
    except HTTPException:
        conn.commit()  # 争用留痕落库可查，随后整笔拒收
        raise
    acquired = True
    try:
        conn.execute("SAVEPOINT cage_write")
        yield
        conn.execute("RELEASE SAVEPOINT cage_write")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT cage_write")
        conn.execute("RELEASE SAVEPOINT cage_write")
        conn.commit()  # 业务写整笔回滚（零改零增）
        raise
    finally:
        if acquired:
            release(conn, lock_key, holder)
            conn.commit()


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM cage_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cage_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}
