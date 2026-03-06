# app/models.py
from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from datetime import datetime


class ClientConfig(BaseModel):
    id: UUID
    name: str
    whatsapp_number: str
    system_prompt: str
    tools_enabled: list[str]
    sheet_id: Optional[str]
    prompt_version: int
    active: bool


class Message(BaseModel):
    role: str  # 'user' | 'assistant' | 'tool'
    content: str
    tool_name: Optional[str] = None


class ConversationHistory(BaseModel):
    messages: list[Message] = []