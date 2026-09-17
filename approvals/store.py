"""Approval records in Redis.

One hash per approval plus a pending index. Values are JSON-encoded because an
approval's arguments are an arbitrary tool payload.
"""
import json
import time
import uuid

# redis-py — the standard Python Redis client. Redis runs as its own pod here.
import redis

from config import Config

_r = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)

PENDING_KEY = "o11yag:approvals:pending"


def _key(approval_id: str) -> str:
    return f"o11yag:approval:{approval_id}"


def create(tool: str, args: dict, ticket_id: str, customer_id: str, reason: str) -> dict:
    approval_id = uuid.uuid4().hex[:12]
    record = {
        "approval_id": approval_id, "tool": tool, "args": json.dumps(args),
        "ticket_id": ticket_id or "", "customer_id": customer_id or "",
        "reason": reason or "", "status": "pending",
        "requested_at": str(time.time()), "decided_by": "", "decided_at": "",
    }
    pipe = _r.pipeline()
    pipe.hset(_key(approval_id), mapping=record)
    pipe.expire(_key(approval_id), Config.TTL_S)
    pipe.sadd(PENDING_KEY, approval_id)
    pipe.execute()
    return _hydrate(record)


def get(approval_id: str):
    record = _r.hgetall(_key(approval_id))
    return _hydrate(record) if record else None


def decide(approval_id: str, decision: str, by: str):
    if not _r.exists(_key(approval_id)):
        return None
    pipe = _r.pipeline()
    pipe.hset(_key(approval_id), mapping={
        "status": decision, "decided_by": by, "decided_at": str(time.time())})
    pipe.srem(PENDING_KEY, approval_id)
    pipe.execute()
    return get(approval_id)


def pending():
    out = [get(aid) for aid in _r.smembers(PENDING_KEY)]
    return sorted([a for a in out if a and a["status"] == "pending"],
                  key=lambda a: a["requested_at"])


def _hydrate(record: dict) -> dict:
    out = dict(record)
    out["args"] = json.loads(out.get("args") or "{}")
    out["requested_at"] = float(out.get("requested_at") or 0)
    out["decided_at"] = float(out["decided_at"]) if out.get("decided_at") else None
    out["waited_s"] = round((out["decided_at"] or time.time()) - out["requested_at"], 1)
    return out
