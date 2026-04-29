from __future__ import annotations

from fastapi import APIRouter, HTTPException

from models import (
    PushAlertRegistrationRequest,
    PushAlertUnregisterRequest,
    PushTestRequest,
)
from services.push_service import (
    get_public_key_payload,
    push_enabled,
    remove_push_alert,
    send_test_push,
    upsert_push_alert,
)


router = APIRouter()


@router.get("/api/push/public-key")
def get_push_public_key() -> dict[str, object]:
    return get_public_key_payload()


@router.post("/api/push/register-alert")
def register_push_alert(request: PushAlertRegistrationRequest) -> dict[str, object]:
    if not push_enabled():
        raise HTTPException(
            status_code=503,
            detail="Push notifications are disabled on backend. Configure VAPID keys.",
        )

    upsert_push_alert(
        request.subscription,
        request.alert,
        reset_last_notified_on=request.reset_last_notified_on,
    )
    return {"ok": True}


@router.post("/api/push/unregister-alert")
def unregister_push_alert(request: PushAlertUnregisterRequest) -> dict[str, object]:
    removed = remove_push_alert(request.endpoint, request.alert_id)
    return {"ok": True, "removed": removed}


@router.post("/api/push/test")
def push_test(request: PushTestRequest) -> dict[str, object]:
    return send_test_push(
        endpoint=request.endpoint,
        subscription_payload=request.subscription,
        title=request.title,
        body=request.body,
    )
