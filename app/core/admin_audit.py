"""Automatic audit trail for every admin write.

`get_current_user` tags the request's DB session when an ADMIN calls a write endpoint;
the session hook below then adds one AdminAuditLog row (who, which operation, path
params, query, redacted JSON body, IP) to the FIRST commit that actually changes data —
the same transaction as the change, so an action can't commit without its audit row
(and a rejected/rolled-back request leaves none). Handlers that already write a richer
record_audit row in that transaction are not double-logged."""
import json
import re

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session

from app.core.clientip import client_ip
from app.models.admin_tools import AdminAuditLog

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SECRET_KEY = re.compile(r"pass|secret|token|otp|signature|key|authorization", re.I)
_MAX_BODY = 4096
_KEY = "admin_write"


def _redact(value):
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if _SECRET_KEY.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value[:50]]
    return value


async def tag_admin_write(request: Request, session, user) -> None:
    if request.method not in _WRITE_METHODS or user.role != "admin":
        return
    route = request.scope.get("route")
    action = (getattr(route, "operation_id", None) or getattr(route, "name", None)
              or request.url.path)[:64]
    detail: dict = {"method": request.method, "path": request.url.path[:255]}
    if request.path_params:
        detail["params"] = {k: str(v) for k, v in request.path_params.items()}
    if request.query_params:
        detail["query"] = _redact(dict(request.query_params))
    if (request.headers.get("content-type") or "").startswith("application/json"):
        raw = await request.body()  # cached by Starlette; the handler already parsed it
        if raw and len(raw) <= _MAX_BODY:
            try:
                detail["body"] = _redact(json.loads(raw))
            except ValueError:
                pass
        elif raw:
            detail["body_bytes"] = len(raw)
    resource_id = next(
        (str(v) for k, v in reversed(list(request.path_params.items())) if k.endswith("id")), None
    )
    parts = [p for p in request.url.path.split("/") if p and p not in ("api", "v1", "admin")]
    session.info[_KEY] = {
        "admin_user_id": user.id, "action": action,
        "resource_type": (parts[0] if parts else None), "resource_id": resource_id,
        "detail": detail, "ip": client_ip(request), "changed": False, "done": False,
    }


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _ctx) -> None:
    tag = session.info.get(_KEY)
    if tag is None or tag["done"]:
        return
    if any(isinstance(o, AdminAuditLog) for o in session.new):
        tag["done"] = True  # the handler wrote its own audit row in this transaction
    elif session.new or session.dirty or session.deleted:
        tag["changed"] = True


@event.listens_for(Session, "do_orm_execute")
def _bulk_write(state: ORMExecuteState) -> None:
    tag = state.session.info.get(_KEY)
    if tag is not None and not tag["done"] and (state.is_update or state.is_delete or state.is_insert):
        tag["changed"] = True


@event.listens_for(Session, "before_commit")
def _before_commit(session: Session) -> None:
    tag = session.info.get(_KEY)
    if tag is None or tag["done"]:
        return
    if any(isinstance(o, AdminAuditLog) for o in session.new):
        tag["done"] = True
        return
    if not (tag["changed"] or session.new or session.dirty or session.deleted):
        return
    session.add(AdminAuditLog(
        admin_user_id=tag["admin_user_id"], action=tag["action"],
        resource_type=(tag["resource_type"] or "")[:48] or None,
        resource_id=(tag["resource_id"] or "")[:64] or None,
        detail=tag["detail"], ip=tag["ip"],
    ))
    tag["done"] = True
