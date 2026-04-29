from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import APP_DESCRIPTION, APP_TITLE, APP_VERSION, CORS_ALLOW_ORIGINS
from routers.push import router as push_router
from routers.weather import router as weather_router
from services.push_service import load_push_store, push_enabled, start_push_loop, stop_push_loop


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_TITLE,
        description=APP_DESCRIPTION,
        version=APP_VERSION,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def on_startup() -> None:
        load_push_store()
        if push_enabled():
            start_push_loop()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await stop_push_loop()

    app.include_router(weather_router)
    app.include_router(push_router)
    return app


app = create_app()
