from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from models import CitySummary, WeatherResponse
from services.weather_service import (
    build_overview,
    fetch_weather_for_coordinates,
    fetch_weather_for_city,
    list_cities_summary,
    resolve_city,
)


router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "FastAPI backend is running",
        "provider": "Open-Meteo",
        "date": date.today().isoformat(),
    }


@router.get("/api/cities", response_model=list[CitySummary])
def list_cities() -> list[CitySummary]:
    return list_cities_summary()


@router.get("/api/weather", response_model=WeatherResponse)
def get_weather(city: str = Query(..., description="Название города")) -> WeatherResponse:
    resolved_city = resolve_city(city)
    return fetch_weather_for_city(resolved_city)


@router.get("/api/weather/by-coordinates", response_model=WeatherResponse)
def get_weather_by_coordinates(
    latitude: float = Query(..., description="Широта"),
    longitude: float = Query(..., description="Долгота"),
) -> WeatherResponse:
    return fetch_weather_for_coordinates(latitude, longitude)


@router.get("/api/overview")
def overview() -> dict[str, object]:
    return build_overview()
