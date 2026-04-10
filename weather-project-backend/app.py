from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import time
import asyncio
from pathlib import Path
from threading import Lock
from copy import deepcopy
from datetime import date, datetime
from typing import Any, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
    load_pem_public_key,
)
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from pywebpush import WebPushException, webpush
except ImportError:
    WebPushException = Exception
    webpush = None


BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger("weather.push")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def _to_base64url_no_padding(raw_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")


def normalize_vapid_public_key(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
        if len(decoded) == 65 and decoded[0] == 0x04:
            return _to_base64url_no_padding(decoded)
    except (binascii.Error, ValueError):
        pass

    if "BEGIN PUBLIC KEY" in value:
        try:
            public_key = load_pem_public_key(value.encode("utf-8"))
            uncompressed = public_key.public_bytes(
                encoding=Encoding.X962,
                format=PublicFormat.UncompressedPoint,
            )
            return _to_base64url_no_padding(uncompressed)
        except Exception:
            return value

    try:
        der_bytes = base64.b64decode(value, validate=True)
        public_key = load_der_public_key(der_bytes)
        uncompressed = public_key.public_bytes(
            encoding=Encoding.X962,
            format=PublicFormat.UncompressedPoint,
        )
        return _to_base64url_no_padding(uncompressed)
    except Exception:
        return value


FORECAST_API_URL = os.getenv("OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
GEOCODING_API_URL = os.getenv(
    "OPEN_METEO_GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/search"
)
REVERSE_GEOCODING_URL = os.getenv(
    "REVERSE_GEOCODING_URL", "https://nominatim.openstreetmap.org/reverse"
)
REQUEST_TIMEOUT_SECONDS = float(os.getenv("OPEN_METEO_TIMEOUT", "10"))
CACHE_TTL_SECONDS = int(os.getenv("WEATHER_CACHE_TTL", "300"))
PUSH_CHECK_INTERVAL_SECONDS = int(os.getenv("PUSH_CHECK_INTERVAL_SECONDS", "600"))
PUSH_ACTIVATION_DELAY_SECONDS = int(os.getenv("PUSH_ACTIVATION_DELAY_SECONDS", "30"))
VAPID_PUBLIC_KEY = normalize_vapid_public_key(os.getenv("VAPID_PUBLIC_KEY", ""))
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_PRIVATE_KEY_PATH = os.getenv("VAPID_PRIVATE_KEY_PATH", "").strip()
VAPID_CLAIMS_SUBJECT = os.getenv("VAPID_CLAIMS_SUBJECT", "mailto:admin@example.com").strip()
PUSH_STORE_FILE = BASE_DIR / "push_store.json"

if VAPID_PRIVATE_KEY_PATH:
    private_key_path = Path(VAPID_PRIVATE_KEY_PATH)
    if not private_key_path.is_absolute():
        private_key_path = BASE_DIR / private_key_path
    if private_key_path.exists():
        VAPID_PRIVATE_KEY = str(private_key_path)


class CityCatalogItem(TypedDict):
    name: str
    country: str
    latitude: float
    longitude: float


class CacheEntry(TypedDict):
    expires_at: float
    payload: dict


SUPPORTED_CITIES: list[CityCatalogItem] = [
    {"name": "Барнаул", "country": "Россия", "latitude": 53.3474, "longitude": 83.7784},
]

WEATHER_CODE_MAP = {
    0: "Ясно",
    1: "Преимущественно ясно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Изморозь",
    51: "Слабая морось",
    53: "Морось",
    55: "Сильная морось",
    56: "Ледяная морось",
    57: "Сильная ледяная морось",
    61: "Небольшой дождь",
    63: "Дождь",
    65: "Сильный дождь",
    66: "Ледяной дождь",
    67: "Сильный ледяной дождь",
    71: "Небольшой снег",
    73: "Снег",
    75: "Сильный снег",
    77: "Снежные зерна",
    80: "Кратковременный дождь",
    81: "Ливень",
    82: "Сильный ливень",
    85: "Снежный заряд",
    86: "Сильный снежный заряд",
    95: "Гроза",
    96: "Гроза с градом",
    99: "Сильная гроза с градом",
}

DAY_NAMES = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}

WEATHER_CACHE: dict[str, CacheEntry] = {}
PUSH_STORE_LOCK = Lock()
PUSH_LOOP_TASK: asyncio.Task | None = None
PUSH_STORE: dict[str, Any] = {
    "subscriptions": {},
    "alerts": [],
}


class ForecastResponse(BaseModel):
    day: str
    condition: str
    min_temp_c: float
    max_temp_c: float
    precipitation_chance: int
    wind_speed_m_s: float


class HourlyForecastResponse(BaseModel):
    time: str
    condition: str
    temperature_c: float
    precipitation_chance: int


class CitySummary(BaseModel):
    name: str
    country: str
    condition: str
    temperature_c: float
    latitude: float
    longitude: float


class WeatherResponse(BaseModel):
    city: str
    country: str
    latitude: float
    longitude: float
    updated_at: str
    condition: str
    temperature_c: float
    feels_like_c: float
    humidity: int
    wind_speed: float
    pressure_mmhg: int
    visibility_km: float
    tomorrow_metrics: dict[str, float | int]
    forecast: list[ForecastResponse]
    hourly_forecast: list[HourlyForecastResponse]


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionPayload(BaseModel):
    endpoint: str
    expirationTime: float | None = None
    keys: PushSubscriptionKeys


class PushAlertPreferences(BaseModel):
    precipitation: bool = True
    humidity: bool = True
    wind: bool = True
    pressure: bool = True
    visibility: bool = True


class PushAlertPayload(BaseModel):
    id: str
    label: str
    country: str
    latitude: float
    longitude: float
    preferences: PushAlertPreferences = Field(default_factory=PushAlertPreferences)


class PushAlertRegistrationRequest(BaseModel):
    subscription: PushSubscriptionPayload
    alert: PushAlertPayload
    reset_last_notified_on: bool = False


class PushAlertUnregisterRequest(BaseModel):
    endpoint: str
    alert_id: str


class PushTestRequest(BaseModel):
    endpoint: str | None = None
    subscription: PushSubscriptionPayload | None = None
    title: str = "Тест push-уведомления"
    body: str = "Проверка канала push: если вы видите это сообщение, то уведомления работают."


def get_cached_payload(cache_key: str) -> dict | None:
    cached_entry = WEATHER_CACHE.get(cache_key)
    if cached_entry is None:
        return None

    if cached_entry["expires_at"] <= time.time():
        WEATHER_CACHE.pop(cache_key, None)
        return None

    return deepcopy(cached_entry["payload"])


def set_cached_payload(cache_key: str, payload: dict) -> None:
    WEATHER_CACHE[cache_key] = {
        "expires_at": time.time() + CACHE_TTL_SECONDS,
        "payload": deepcopy(payload),
    }


def fetch_json(base_url: str, params: dict[str, object], *, cache_key: str | None = None) -> dict:
    if cache_key is not None:
        cached_payload = get_cached_payload(cache_key)
        if cached_payload is not None:
            return cached_payload

    request_url = f"{base_url}?{urlencode(params, doseq=True)}"
    request = Request(
        request_url,
        headers={
            "User-Agent": "weather-service-demo/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if cache_key is not None:
                set_cached_payload(cache_key, payload)
            return payload
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Погодный провайдер вернул ошибку: {exc.code}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail="Не удалось связаться с погодным провайдером") from exc


def weather_code_to_text(code: int | None) -> str:
    if code is None:
        return "Нет данных"

    return WEATHER_CODE_MAP.get(code, "Неизвестные погодные условия")


def hpa_to_mmhg(value: float) -> int:
    return round(value * 0.750062)


def kmh_to_ms(value: float) -> float:
    return round(value / 3.6, 1)


def ceil_temperature(value: float) -> float:
    return float(math.ceil(value))


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


def format_notification_temperature(value: float) -> str:
    rounded_value = int(math.ceil(value))
    sign = "+" if rounded_value > 0 else ""
    return f"{sign}{rounded_value}°C"


def format_day_label(index: int, iso_date: str) -> str:
    if index == 0:
        return "Сегодня"
    if index == 1:
        return "Завтра"

    weekday_index = date.fromisoformat(iso_date).weekday()
    return DAY_NAMES[weekday_index]


def format_updated_at(value: str) -> str:
    if not value:
        return ""

    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def format_hour_label(iso_datetime: str) -> str:
    if not iso_datetime:
        return ""

    try:
        return datetime.fromisoformat(iso_datetime).strftime("%H:%M")
    except ValueError:
        if "T" in iso_datetime:
            return iso_datetime.split("T", 1)[1][:5]
        return iso_datetime[:5]


def build_hourly_forecast(payload: dict, current_time: str) -> list[HourlyForecastResponse]:
    hourly = payload.get("hourly", {})
    hourly_times = hourly.get("time", [])
    hourly_codes = hourly.get("weather_code", [])
    hourly_temps = hourly.get("temperature_2m", [])
    hourly_precipitation = hourly.get("precipitation_probability", [])

    if not hourly_times or not hourly_temps:
        return []

    start_index = 0
    if current_time and current_time in hourly_times:
        start_index = hourly_times.index(current_time)
    elif current_time:
        for index, hour_value in enumerate(hourly_times):
            if hour_value >= current_time:
                start_index = index
                break

    end_index = min(start_index + 24, len(hourly_times), len(hourly_temps))
    result: list[HourlyForecastResponse] = []

    for index in range(start_index, end_index):
        result.append(
            HourlyForecastResponse(
                time=format_hour_label(hourly_times[index]),
                condition=weather_code_to_text(
                    hourly_codes[index] if index < len(hourly_codes) else None
                ),
                temperature_c=ceil_temperature(float(hourly_temps[index])),
                precipitation_chance=int(
                    hourly_precipitation[index] if index < len(hourly_precipitation) else 0
                ),
            )
        )

    return result


def build_tomorrow_metrics(payload: dict) -> dict[str, float | int]:
    daily = payload.get("daily", {})
    daily_times = daily.get("time", [])
    tomorrow_date = daily_times[1] if len(daily_times) > 1 else None
    daily_precip = daily.get("precipitation_probability_max", [])

    metrics: dict[str, float | int] = {
        "precipitation_chance": int(daily_precip[1] or 0) if len(daily_precip) > 1 else 0,
        "humidity": 0,
        "wind_speed_m_s": 0.0,
        "pressure_mmhg": 0,
        "visibility_km": 0.0,
    }

    if not tomorrow_date:
        return metrics

    hourly = payload.get("hourly", {})
    hourly_times = hourly.get("time", [])
    if not isinstance(hourly_times, list) or not hourly_times:
        return metrics

    tomorrow_indices = [
        idx for idx, iso_time in enumerate(hourly_times) if str(iso_time).startswith(f"{tomorrow_date}T")
    ]
    if not tomorrow_indices:
        return metrics

    def _hour_from_iso(iso_value: str) -> int:
        try:
            return int(str(iso_value).split("T", 1)[1].split(":", 1)[0])
        except Exception:
            return 0

    reference_index = min(
        tomorrow_indices,
        key=lambda idx: abs(_hour_from_iso(str(hourly_times[idx])) - 12),
    )

    humidity_values = hourly.get("relative_humidity_2m", [])
    wind_values = hourly.get("wind_speed_10m", [])
    pressure_values = hourly.get("pressure_msl", [])
    visibility_values = hourly.get("visibility", [])

    if reference_index < len(humidity_values):
        metrics["humidity"] = int(humidity_values[reference_index] or 0)
    if reference_index < len(wind_values):
        metrics["wind_speed_m_s"] = kmh_to_ms(float(wind_values[reference_index] or 0))
    if reference_index < len(pressure_values):
        metrics["pressure_mmhg"] = hpa_to_mmhg(float(pressure_values[reference_index] or 0))
    if reference_index < len(visibility_values):
        metrics["visibility_km"] = round(float(visibility_values[reference_index] or 0) / 1000, 1)

    return metrics


def resolve_city(name: str) -> CityCatalogItem:
    normalized_name = name.strip().lower()

    for city in SUPPORTED_CITIES:
        if city["name"].lower() == normalized_name:
            return city

    payload = fetch_json(
        GEOCODING_API_URL,
        {
            "name": name.strip(),
            "count": 1,
            "language": "ru",
            "format": "json",
        },
        cache_key=f"forward:{normalized_name}",
    )
    results = payload.get("results") or []

    if not results:
        raise HTTPException(status_code=404, detail="Город не найден")

    first_result = results[0]
    return {
        "name": first_result["name"],
        "country": first_result.get("country", ""),
        "latitude": float(first_result["latitude"]),
        "longitude": float(first_result["longitude"]),
    }


def reverse_geocode(latitude: float, longitude: float) -> tuple[str, str]:
    payload = fetch_json(
        REVERSE_GEOCODING_URL,
        {
            "lat": latitude,
            "lon": longitude,
            "format": "jsonv2",
            "zoom": 10,
            "addressdetails": 1,
        },
        cache_key=f"reverse:{latitude:.4f}:{longitude:.4f}",
    )

    address = payload.get("address", {})
    city_name = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or payload.get("name")
        or "Точка на карте"
    )
    country_name = address.get("country") or "Неизвестная страна"
    return str(city_name), str(country_name)


def normalize_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    if latitude < -90 or latitude > 90:
        raise HTTPException(status_code=400, detail="Широта должна быть в диапазоне от -90 до 90")

    if longitude < -180 or longitude > 180:
        raise HTTPException(status_code=400, detail="Долгота должна быть в диапазоне от -180 до 180")

    return round(latitude, 4), round(longitude, 4)


def build_forecast(city: CityCatalogItem, payload: dict) -> WeatherResponse:
    current = payload.get("current", {})
    daily = payload.get("daily", {})
    daily_times = daily.get("time", [])
    daily_codes = daily.get("weather_code", [])
    daily_min_temps = daily.get("temperature_2m_min", [])
    daily_max_temps = daily.get("temperature_2m_max", [])
    daily_precip = daily.get("precipitation_probability_max", [])
    daily_wind_speeds = daily.get("wind_speed_10m_max", [])
    hourly_forecast = build_hourly_forecast(payload, current.get("time", ""))

    forecast = [
        ForecastResponse(
            day=format_day_label(index, day_value),
            condition=weather_code_to_text(daily_codes[index] if index < len(daily_codes) else None),
            min_temp_c=ceil_temperature(float(daily_min_temps[index])),
            max_temp_c=ceil_temperature(float(daily_max_temps[index])),
            precipitation_chance=int(daily_precip[index] or 0),
            wind_speed_m_s=kmh_to_ms(
                float(daily_wind_speeds[index]) if index < len(daily_wind_speeds) else 0
            ),
        )
        for index, day_value in enumerate(daily_times[:8])
    ]

    return WeatherResponse(
        city=city["name"],
        country=city["country"],
        latitude=city["latitude"],
        longitude=city["longitude"],
        updated_at=format_updated_at(current.get("time", "")),
        condition=weather_code_to_text(current.get("weather_code")),
        temperature_c=ceil_temperature(float(current.get("temperature_2m", 0))),
        feels_like_c=ceil_temperature(float(current.get("apparent_temperature", 0))),
        humidity=int(current.get("relative_humidity_2m", 0)),
        wind_speed=kmh_to_ms(float(current.get("wind_speed_10m", 0))),
        pressure_mmhg=hpa_to_mmhg(float(current.get("pressure_msl", 0))),
        visibility_km=round(float(current.get("visibility", 0)) / 1000, 1),
        tomorrow_metrics=build_tomorrow_metrics(payload),
        forecast=forecast,
        hourly_forecast=hourly_forecast,
    )


def fetch_weather_payload(latitude: float, longitude: float) -> dict:
    return fetch_json(
        FORECAST_API_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": 8,
            "current": [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "weather_code",
                "pressure_msl",
                "visibility",
                "wind_speed_10m",
            ],
            "daily": [
                "weather_code",
                "temperature_2m_min",
                "temperature_2m_max",
                "precipitation_probability_max",
                "wind_speed_10m_max",
            ],
            "hourly": [
                "temperature_2m",
                "weather_code",
                "precipitation_probability",
                "relative_humidity_2m",
                "wind_speed_10m",
                "pressure_msl",
                "visibility",
            ],
        },
        cache_key=f"weather:{latitude:.4f}:{longitude:.4f}",
    )


def fetch_weather_for_city(city: CityCatalogItem) -> WeatherResponse:
    payload = fetch_weather_payload(city["latitude"], city["longitude"])
    return build_forecast(city, payload)


def fetch_weather_for_coordinates(latitude: float, longitude: float) -> WeatherResponse:
    normalized_latitude, normalized_longitude = normalize_coordinates(latitude, longitude)
    payload = fetch_weather_payload(normalized_latitude, normalized_longitude)
    city_name, country_name = reverse_geocode(normalized_latitude, normalized_longitude)

    location = {
        "name": city_name,
        "country": country_name,
        "latitude": normalized_latitude,
        "longitude": normalized_longitude,
    }
    return build_forecast(location, payload)


def push_enabled() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and webpush is not None)


def load_push_store() -> None:
    global PUSH_STORE
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

        with PUSH_STORE_LOCK:
            PUSH_STORE = {
                "subscriptions": subscriptions,
                "alerts": alerts,
            }
    except (OSError, json.JSONDecodeError):
        return


def save_push_store() -> None:
    with PUSH_STORE_LOCK:
        payload = deepcopy(PUSH_STORE)

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

    with PUSH_STORE_LOCK:
        PUSH_STORE["subscriptions"][subscription.endpoint] = subscription.model_dump()
        current_alerts = PUSH_STORE["alerts"]
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
    with PUSH_STORE_LOCK:
        current_alerts = PUSH_STORE["alerts"]
        filtered_alerts = [
            item
            for item in current_alerts
            if not (item.get("endpoint") == endpoint and item.get("alert_id") == alert_id)
        ]
        removed = len(filtered_alerts) != len(current_alerts)
        PUSH_STORE["alerts"] = filtered_alerts

    if removed:
        save_push_store()
    return removed


def remove_subscription(endpoint: str) -> None:
    with PUSH_STORE_LOCK:
        PUSH_STORE["subscriptions"].pop(endpoint, None)
        PUSH_STORE["alerts"] = [
            item for item in PUSH_STORE["alerts"] if item.get("endpoint") != endpoint
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
        details.append(f"Вероятность осадков {int(tomorrow_metrics.get('precipitation_chance', 0))}%")
    if preferences["humidity"]:
        details.append(f"Влажность {int(tomorrow_metrics.get('humidity', 0))}%")
    if preferences["wind"]:
        details.append(f"Ветер {math.ceil(float(tomorrow_metrics.get('wind_speed_m_s', 0)))} м/с")
    if preferences["pressure"]:
        details.append(f"Давление {int(tomorrow_metrics.get('pressure_mmhg', 0))} мм рт. ст.")
    if preferences["visibility"]:
        visibility_value = f"{float(tomorrow_metrics.get('visibility_km', 0.0)):.1f}".rstrip("0").rstrip(".")
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

    with PUSH_STORE_LOCK:
        alerts_snapshot = deepcopy(PUSH_STORE["alerts"])
        subscriptions_snapshot = deepcopy(PUSH_STORE["subscriptions"])

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

            with PUSH_STORE_LOCK:
                for stored_alert in PUSH_STORE["alerts"]:
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


app = FastAPI(
    title="Weather Service API",
    description="FastAPI-прослойка над Open-Meteo для погодного сервиса.",
    version="3.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://hilarious-lollipop-81bb12.netlify.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup() -> None:
    global PUSH_LOOP_TASK
    load_push_store()
    if push_enabled():
        PUSH_LOOP_TASK = asyncio.create_task(push_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global PUSH_LOOP_TASK
    if PUSH_LOOP_TASK is not None:
        PUSH_LOOP_TASK.cancel()
        try:
            await PUSH_LOOP_TASK
        except asyncio.CancelledError:
            pass
        PUSH_LOOP_TASK = None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "FastAPI backend is running",
        "provider": "Open-Meteo",
        "date": date.today().isoformat(),
    }


@app.get("/api/push/public-key")
def get_push_public_key() -> dict[str, object]:
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


@app.post("/api/push/register-alert")
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


@app.post("/api/push/unregister-alert")
def unregister_push_alert(request: PushAlertUnregisterRequest) -> dict[str, object]:
    removed = remove_push_alert(request.endpoint, request.alert_id)
    return {"ok": True, "removed": removed}


@app.post("/api/push/test")
def push_test(request: PushTestRequest) -> dict[str, object]:
    if not push_enabled():
        raise HTTPException(
            status_code=503,
            detail="Push notifications are disabled on backend. Configure VAPID keys.",
        )

    with PUSH_STORE_LOCK:
        subscriptions = deepcopy(PUSH_STORE["subscriptions"])

    targets: list[tuple[str, dict[str, Any]]] = []
    if request.subscription is not None:
        if hasattr(request.subscription, "model_dump"):
            subscription_payload = request.subscription.model_dump()
        else:
            subscription_payload = request.subscription.dict()
        endpoint = request.subscription.endpoint
        targets.append((endpoint, subscription_payload))
        with PUSH_STORE_LOCK:
            PUSH_STORE["subscriptions"][endpoint] = subscription_payload
        save_push_store()
    elif request.endpoint:
        subscription = subscriptions.get(request.endpoint)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription endpoint not found")
        targets.append((request.endpoint, subscription))
    else:
        if not subscriptions:
            raise HTTPException(status_code=404, detail="No push subscriptions found")
        targets = list(subscriptions.items())

    sent_count = 0
    failed_count = 0
    for endpoint, subscription in targets:
        try:
            send_web_push(
                subscription,
                title=request.title,
                body=request.body,
                tag="manual-push-test",
            )
            sent_count += 1
        except WebPushException:
            failed_count += 1
            remove_subscription(endpoint)
        except Exception:
            failed_count += 1

    return {
        "ok": True,
        "sent": sent_count,
        "failed": failed_count,
        "targeted": len(targets),
    }


@app.get("/api/cities", response_model=list[CitySummary])
def list_cities() -> list[CitySummary]:
    city_summaries: list[CitySummary] = []

    for city in SUPPORTED_CITIES:
        weather = fetch_weather_for_city(city)
        city_summaries.append(
            CitySummary(
                name=weather.city,
                country=weather.country,
                condition=weather.condition,
                temperature_c=weather.temperature_c,
                latitude=weather.latitude,
                longitude=weather.longitude,
            )
        )

    return city_summaries


@app.get("/api/weather", response_model=WeatherResponse)
def get_weather(city: str = Query(..., description="Название города")) -> WeatherResponse:
    resolved_city = resolve_city(city)
    return fetch_weather_for_city(resolved_city)


@app.get("/api/weather/by-coordinates", response_model=WeatherResponse)
def get_weather_by_coordinates(
    latitude: float = Query(..., description="Широта"),
    longitude: float = Query(..., description="Долгота"),
) -> WeatherResponse:
    return fetch_weather_for_coordinates(latitude, longitude)


@app.get("/api/overview")
def overview() -> dict[str, object]:
    city_weather = [fetch_weather_for_city(city) for city in SUPPORTED_CITIES]
    warmest = max(city_weather, key=lambda item: item.temperature_c)

    return {
        "title": "Погодная сводка",
        "description": "Актуальные данные Open-Meteo по нескольким городам и прогноз для любой точки по клику на карте.",
        "cities_count": len(city_weather),
        "highlight": {
            "city": warmest.city,
            "temperature_c": warmest.temperature_c,
            "condition": warmest.condition,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
