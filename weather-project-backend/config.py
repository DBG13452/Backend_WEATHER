from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
    load_pem_public_key,
)

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


APP_TITLE = "Weather Service API"
APP_DESCRIPTION = "FastAPI-прослойка над Open-Meteo для погодного сервиса."
APP_VERSION = "3.1.0"

CORS_ALLOW_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://hilarious-lollipop-81bb12.netlify.app",
    "https://dbg13452.github.io",
]

FORECAST_API_URL = os.getenv("OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
GEOCODING_API_URL = os.getenv(
    "OPEN_METEO_GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/search"
)
REVERSE_GEOCODING_URL = os.getenv(
    "REVERSE_GEOCODING_URL", "https://nominatim.openstreetmap.org/reverse"
)
OPEN_METEO_REVERSE_GEOCODING_URL = os.getenv(
    "OPEN_METEO_REVERSE_GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/reverse"
)
BIG_DATA_CLOUD_REVERSE_GEOCODING_URL = os.getenv(
    "BIG_DATA_CLOUD_REVERSE_GEOCODING_URL",
    "https://api.bigdatacloud.net/data/reverse-geocode-client",
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
