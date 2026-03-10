from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.webhook.handler import handle_message
from app.dependencies import (
    get_client_service,
    get_conversation_context,
    get_conversation_service,
    get_whatsapp_client,
    get_sheets_client,
    get_transcriber_client,
)

router = APIRouter(prefix="/webhook")

@router.get("")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verification token")

@router.post("")
async def receive_message(
    request: Request,
    background_tasks: BackgroundTasks,
    client_service=Depends(get_client_service),
    conversation_context=Depends(get_conversation_context),
    conversation_service=Depends(get_conversation_service),
    whatsapp_client=Depends(get_whatsapp_client),
    sheets_client=Depends(get_sheets_client),
    transcriber_client=Depends(get_transcriber_client),
):
    payload = await request.json()
    
    background_tasks.add_task(
        handle_message,
        payload=payload,
        background_tasks=background_tasks,
        get_client_by_phone=client_service.get_client_by_phone,
        conversation_service=conversation_service,
        whatsapp_client=whatsapp_client,
        sheets_client=sheets_client,
        transcriber_client=transcriber_client,
    )
    
    return {"status": "ok"}
