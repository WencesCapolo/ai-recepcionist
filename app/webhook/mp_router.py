from fastapi import APIRouter, Depends, Request
from upstash_redis import Redis

from app.clients.service import ClientService
from app.dependencies import get_redis, get_client_service, get_whatsapp_client
from app.integrations.whatsapp import WhatsAppClient
from app.webhook.mp_handler import handle_mp_notification

router = APIRouter()


@router.post("/mp-webhook")
async def mp_webhook(
    request: Request,
    redis: Redis = Depends(get_redis),
    client_service: ClientService = Depends(get_client_service),
    whatsapp: WhatsAppClient = Depends(get_whatsapp_client),
) -> dict:
    # Always return 200 immediately — MP retries on any other status
    try:
        body = await request.json()
        await handle_mp_notification(
            body=body,
            redis=redis,
            client_service=client_service,
            whatsapp=whatsapp,
        )
    except Exception:
        pass  # Logged inside handler, never let MP see a 500

    return {"status": "ok"}