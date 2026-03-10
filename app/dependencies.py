from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from supabase import create_client, Client
from upstash_redis import Redis

from app.config import settings
from app.clients.service import ClientService
from app.context.redis import ConversationContext
from app.conversations.service import ConversationService
from app.integrations.whatsapp import get_whatsapp_client
from app.integrations.sheets import get_sheets_client
from app.integrations.transcriber import get_transcriber_client

@lru_cache
def get_supabase() -> Client:
    return create_client(settings.supabase_url, settings.supabase_service_key)

@lru_cache
def get_redis() -> Redis:
    return Redis(url=settings.UPSTASH_REDIS_REST_URL, token=settings.UPSTASH_REDIS_REST_TOKEN)

def get_client_service(
    supabase: Annotated[Client, Depends(get_supabase)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ClientService:
    return ClientService(supabase=supabase, redis=redis)

def get_conversation_context(
    redis: Annotated[Redis, Depends(get_redis)],
) -> ConversationContext:
    return ConversationContext(redis=redis)

def get_conversation_service(
    supabase: Annotated[Client, Depends(get_supabase)],
) -> ConversationService:
    return ConversationService(supabase=supabase)