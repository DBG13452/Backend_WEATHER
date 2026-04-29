from __future__ import annotations

import asyncio
import json
import math
import time
from copy import deepcopy
from datetime import date
from typing import Any

from fastapi import HTTPException

import state
from config import (
    PUSH_ACTIVATION_DELAY_SECONDS,
    PUSH_CHECK_INTERVAL_SECONDS,
    PUSH_STORE_FILE,
    VAPID_CLAIMS_SUBJECT,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
    WebPushException,
    logger,
    webpush,
)
from models import PushAlertPayload, PushSubscriptionPayload
from services.weather_service import (
    build_forecast,
    fetch_weather_payload,
    format_notification_temperature,
)


DEFAULT_ALERT_PREFERENCES: dict[str, bool] = {
    "precipitation": True,
    "humidity": True,
    "wind": True,
    "pressure": True,
    "visibility": True,
}


def normalize_alert_preferences(raw_value: Any) -> dict[str, bool]:
    normalized_preferences = dict(DEFAULT_ALERT_PREFERENCES)
    if not isinstance(raw_value, dict):
        return normalized_preferences

    if (
        isinstance(raw_value.get("feels_like"), bool)
        and not isinstance(raw_value.get("precipitation"), bool)
    ):
        normalized_preferences["precipitation"] = bool(raw_value.get("feels_like"))

    for key in normalized_preferences:
        candidate = raw_value.get(key)
        if isinstance(candidate, bool):
            normalized_preferences[key] = candidate

    return normalized_preferences


def push_enabled() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and webpush is not None)


def load_push_store() -> None:
    if not PUSH_STORE_FILE.exists():
        return

    try:
        raw_content = PUSH_STORE_FILE.read_text(encoding="utf-8")
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            return

        subscriptions = parsed.get("subscriptions", {})
        alerts = parsed.get("alerts", [])
        if not isinstance(subscriptions, dict):
            subscriptions = {}
        if not isinstance(alerts, list):
            alerts = []

        with state.PUSH_STORE_LOCK:
            state.PUSH_STORE = {
                "subscriptions": subscriptions,
                "alerts": alerts,
            }
    except (OSError, json.JSONDecodeError):
        return


def save_push_store() -> None:
    with state.PUSH_STORE_LOCK:
        payload = deepcopy(state.PUSH_STORE)

    try:
        PUSH_STORE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def upsert_push_alert(
    subscription: PushSubscriptionPayload,
    alert: PushAlertPayload,
    *,
    reset_last_notified_on: bool = False,
) -> None:
    activation_delay_seconds = max(PUSH_ACTIVATION_DELAY_SECONDS, 0)
    send_not_before_ts: float | None = None
    if reset_last_notified_on:
        send_not_before_ts = time.time() + activation_delay_seconds

    normalized_alert = {
        "endpoint": subscription.endpoint,
        "alert_id": alert.id,
        "label": alert.label,
        "country": alert.country,
        "latitude": round(alert.latitude, 4),
        "longitude": round(alert.longitude, 4),
        "last_notified_on": None,
        "preferences": normalize_alert_preferences(alert.preferences.model_dump()),
        "send_not_before_ts": send_not_before_ts,
    }

    with state.PUSH_STORE_LOCK:
        state.PUSH_STORE["subscriptions"][subscription.endpoint] = subscription.model_dump()
        current_alerts = state.PUSH_STORE["alerts"]
        for index, item in enumerate(current_alerts):
            if (
                item.get("endpoint") == subscription.endpoint
                and item.get("alert_id") == alert.id
            ):
                if reset_last_notified_on:
                    normalized_alert["last_notified_on"] = None
                else:
                    normalized_alert["last_notified_on"] = item.get("last_notified_on")
                    normalized_alert["send_not_before_ts"] = item.get("send_not_before_ts")
                current_alerts[index] = normalized_alert
                break
        else:
            current_alerts.append(normalized_alert)

    save_push_store()


def remove_push_alert(endpoint: str, alert_id: str) -> bool:
    removed = False
    with state.PUSH_STORE_LOCK:
        current_alerts = state.PUSH_STORE["alerts"]
        filtered_alerts = [
            item
            for item in current_alerts
            if not (item.get("endpoint") == endpoint and item.get("alert_id") == alert_id)
        ]
        removed = len(filtered_alerts) != len(current_alerts)
        state.PUSH_STORE["alerts"] = filtered_alerts

    if removed:
        save_push_store()
    return removed


def remove_subscription(endpoint: str) -> None:
    with state.PUSH_STORE_LOCK:
        state.PUSH_STORE["subscriptions"].pop(endpoint, None)
        state.PUSH_STORE["alerts"] = [
            item for item in state.PUSH_STORE["alerts"] if item.get("endpoint") != endpoint
        ]

    save_push_store()


def build_tomorrow_notification(alert_item: dict[str, Any]) -> str | None:
    city = {
        "name": str(alert_item.get("label") or "Точка"),
        "country": str(alert_item.get("country") or ""),
        "latitude": float(alert_item["latitude"]),
        "longitude": float(alert_item["longitude"]),
    }
    payload = fetch_weather_payload(city["latitude"], city["longitude"])
    forecast = build_forecast(city, payload)
    if len(forecast.forecast) < 2:
        return None

    tomorrow = forecast.forecast[1]
    tomorrow_metrics = forecast.tomorrow_metrics
    preferences = normalize_alert_preferences(alert_item.get("preferences"))
    details: list[str] = []

    if preferences["precipitation"]:
        details.append(
            f"Вероятность осадков {int(tomorrow_metrics.get('precipitation_chance', 0))}%"
        )
    if preferences["humidity"]:
        details.append(f"Влажность {int(tomorrow_metrics.get('humidity', 0))}%")
    if preferences["wind"]:
        details.append(
            f"Ветер {math.ceil(float(tomorrow_metrics.get('wind_speed_m_s', 0)))} м/с"
        )
    if preferences["pressure"]:
        details.append(
            f"Давление {int(tomorrow_metrics.get('pressure_mmhg', 0))} мм рт. ст."
        )
    if preferences["visibility"]:
        visibility_value = (
            f"{float(tomorrow_metrics.get('visibility_km', 0.0)):.1f}".rstrip("0").rstrip(".")
        )
        details.append(f"Видимость {visibility_value} км")

    base_text = (
        f"{city['name']}: завтра {tomorrow.condition.lower()}. "
        f"Днем до {format_notification_temperature(tomorrow.max_temp_c)}, "
        f"Ночью до {format_notification_temperature(tomorrow.min_temp_c)}"
    )
    if details:
        return f"{base_text}. {', '.join(details)}"
    return base_text


def send_web_push(subscription: dict[str, Any], *, title: str, body: str, tag: str) -> None:
    webpush(
        subscription_info=subscription,
        data=json.dumps(
            {
                "title": title,
                "body": body,
                "tag": tag,
            },
            ensure_ascii=False,
        ),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_CLAIMS_SUBJECT},
    )


def process_push_notifications_once() -> None:
    if not push_enabled():
        return

    with state.PUSH_STORE_LOCK:
        alerts_snapshot = deepcopy(state.PUSH_STORE["alerts"])
        subscriptions_snapshot = deepcopy(state.PUSH_STORE["subscriptions"])

    if not alerts_snapshot:
        return

    today_key = date.today().isoformat()
    should_save = False

    for alert_item in alerts_snapshot:
        endpoint = str(alert_item.get("endpoint") or "")
        if not endpoint:
            continue

        send_not_before_ts = alert_item.get("send_not_before_ts")
        if isinstance(send_not_before_ts, (int, float)) and time.time() < float(send_not_before_ts):
            continue

        if alert_item.get("last_notified_on") == today_key:
            continue

        subscription = subscriptions_snapshot.get(endpoint)
        if not subscription:
            remove_subscription(endpoint)
            should_save = True
            continue

        try:
            message = build_tomorrow_notification(alert_item)
            if not message:
                continue

            send_web_push(
                subscription,
                title="Прогноз на завтра",
                body=message,
                tag=f"tomorrow-{alert_item.get('alert_id', '')}",
            )

            with state.PUSH_STORE_LOCK:
                for stored_alert in state.PUSH_STORE["alerts"]:
                    if (
                        stored_alert.get("endpoint") == endpoint
                        and stored_alert.get("alert_id") == alert_item.get("alert_id")
                    ):
                        stored_alert["last_notified_on"] = today_key
                        stored_alert["send_not_before_ts"] = None
                        break
            should_save = True
        except WebPushException:
            remove_subscription(endpoint)
            should_save = True
            logger.warning(
                "Push subscription removed after WebPushException: alert_id=%s endpoint=%s",
                alert_item.get("alert_id"),
                endpoint,
            )
        except Exception as exc:
            logger.warning(
                "Push delivery skipped: alert_id=%s endpoint=%s reason=%s",
                alert_item.get("alert_id"),
                endpoint,
                exc,
            )
            continue

    if should_save:
        save_push_store()


async def push_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(process_push_notifications_once)
        except Exception as exc:
            logger.exception("Push loop iteration failed: %s", exc)
        await asyncio.sleep(PUSH_CHECK_INTERVAL_SECONDS)


def start_push_loop() -> None:
    if state.PUSH_LOOP_TASK is None:
        state.PUSH_LOOP_TASK = asyncio.create_task(push_loop())


async def stop_push_loop() -> None:
    if state.PUSH_LOOP_TASK is None:
        return

    state.PUSH_LOOP_TASK.cancel()
    try:
        await state.PUSH_LOOP_TASK
    except asyncio.CancelledError:
        pass
    finally:
        state.PUSH_LOOP_TASK = None


def get_public_key_payload() -> dict[str, object]:
    if webpush is None:
        return {
            "enabled": False,
            "reason": "pywebpush is not installed",
            "public_key": None,
        }
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return {
            "enabled": False,
            "reason": "VAPID keys are not configured",
            "public_key": None,
        }

    return {
        "enabled": True,
        "reason": None,
        "public_key": VAPID_PUBLIC_KEY,
    }


def send_test_push(
    *,
    endpoint: str | None,
    subscription_payload: PushSubscriptionPayload | None,
    title: str,
    body: str,
) -> dict[str, object]:
    if not push_enabled():
        raise HTTPException(
            status_code=503,
            detail="Push notifications are disabled on backend. Configure VAPID keys.",
        )

    with state.PUSH_STORE_LOCK:
        subscriptions = deepcopy(state.PUSH_STORE["subscriptions"])

    targets: list[tuple[str, dict[str, Any]]] = []
    if subscription_payload is not None:
        if hasattr(subscription_payload, "model_dump"):
            subscription_data = subscription_payload.model_dump()
        else:
            subscription_data = subscription_payload.dict()
        target_endpoint = subscription_payload.endpoint
        targets.append((target_endpoint, subscription_data))
        with state.PUSH_STORE_LOCK:
            state.PUSH_STORE["subscriptions"][target_endpoint] = subscription_data
        save_push_store()
    elif endpoint:
        subscription = subscriptions.get(endpoint)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription endpoint not found")
        targets.append((endpoint, subscription))
    else:
        if not subscriptions:
            raise HTTPException(status_code=404, detail="No push subscriptions found")
        targets = list(subscriptions.items())

    sent_count = 0
    failed_count = 0
    for target_endpoint, subscription in targets:
        try:
            send_web_push(
                subscription,
                title=title,
                body=body,
                tag="manual-push-test",
            )
            sent_count += 1
        except WebPushException:
            failed_count += 1
            remove_subscription(target_endpoint)
        except Exception:
            failed_count += 1

    return {
        "ok": True,
        "sent": sent_count,
        "failed": failed_count,
        "targeted": len(targets),
    }
