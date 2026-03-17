# app/clients/models.py
from pydantic import BaseModel
from typing import Optional
from uuid import UUID

class ClientConfig(BaseModel):
    id: UUID
    name: str
    whatsapp_number: str
    system_prompt: str
    tools_enabled: list[str]
    sheet_id: Optional[str]
    prompt_version: int
    active: bool
    mp_access_token: Optional[str] = None
    mp_sandbox: bool = True
    # Google Calendar — set to the calendar ID (e.g. "foo@gmail.com")
    # in the Supabase clients table to enable real Calendar integration.
    calendar_id: Optional[str] = None
