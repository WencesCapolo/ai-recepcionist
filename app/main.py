import logging
import logfire
from fastapi import FastAPI
from app.config import settings
from app.webhook.router import router as webhook_router
from app.webhook.mp_router import router as mp_router

def create_app() -> FastAPI:
    if settings.sentry_dsn:
        import sentry_sdk  # type: ignore[import-untyped,import-not-found]
        sentry_sdk.init(dsn=settings.sentry_dsn)
        
    logging.basicConfig(level=settings.log_level)

    if settings.logfire_token:
        logfire.configure(token=settings.logfire_token)
        logfire.instrument_openai()
        logfire.instrument_httpx()

    app_instance = FastAPI()

    if settings.logfire_token:
        logfire.instrument_fastapi(app_instance)
    
    app_instance.include_router(webhook_router)
    app_instance.include_router(mp_router)
    
    @app_instance.get("/health")
    async def health_check():
        return {"status": "ok", "environment": settings.environment}
        
    return app_instance

app = create_app()
