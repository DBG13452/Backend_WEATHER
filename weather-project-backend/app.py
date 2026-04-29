from __future__ import annotations

from main import app
from services.weather_service import SUPPORTED_CITIES


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
