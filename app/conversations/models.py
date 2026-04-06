# app/conversations/models.py
from typing import Literal, Optional
from pydantic import BaseModel, Field

class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_name: Optional[str] = None

class ConversationHistory(BaseModel):
    messages: list[Message] = Field(default_factory=list)
