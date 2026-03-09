import logging
from fastapi import FastAPI
from app.config import settings
from app.webhook.router import router as webhook_router

def create_app() -> FastAPI:
    if settings.sentry_dsn:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.sentry_dsn)
        
    logging.basicConfig(level=settings.log_level)
    
    app_instance = FastAPI()
    
    app_instance.include_router(webhook_router)
    
    @app_instance.get("/health")
    async def health_check():
        return {"status": "ok", "environment": settings.environment}
        
    return app_instance

app = create_app()
