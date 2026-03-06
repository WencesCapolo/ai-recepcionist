# app/conversations/models.py
from pydantic import BaseModel
from typing import Optional

class Message(BaseModel):
    role: str  # 'user' | 'assistant' | 'tool'
    content: str
    tool_name: Optional[str] = None

class ConversationHistory(BaseModel):
    messages: list[Message] = []
