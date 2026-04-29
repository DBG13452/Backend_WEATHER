from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, Field


class CityCatalogItem(TypedDict):
    name: str
    country: str
    latitude: float
    longitude: float


class CacheEntry(TypedDict):
    expires_at: float
    payload: dict


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
    body: str = (
        "Проверка канала push: если вы видите это сообщение, "
        "то уведомления работают."
    )
