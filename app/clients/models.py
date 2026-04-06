# app/clients/models.py
from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator

from app.agent.registry import KNOWN_TOOL_NAMES

class ClientConfig(BaseModel):
    id: UUID
    name: str
    whatsapp_number: str
    system_prompt: str
    tools_enabled: list[str] = Field(default_factory=list)

    @field_validator("tools_enabled")
    @classmethod
    def validate_tool_names(cls, v: list[str]) -> list[str]:
        unknown = set(v) - KNOWN_TOOL_NAMES
        if unknown:
            raise ValueError(f"Unknown tool names: {unknown}")
        return v
    sheet_id: Optional[str] # Google Sheet ID for products/stock (panadería, ferretería, etc)
    prompt_version: int
    active: bool
    mp_access_token: Optional[str] = None
    mp_sandbox: bool = True
    # Google Calendar — set to the calendar ID (e.g. "foo@gmail.com")
    # in the Supabase clients table to enable real Calendar integration.
    calendar_id: Optional[str] = None
    prices_sheet_id: Optional[str] = None  # Google Sheet ID for dentist treatments/insurances
    slot_minutes: int = 30
    work_start_hour: int = 10
    work_end_hour: int = 18
    work_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    sheet_tab_treatments: str = "Tratamientos"
    sheet_tab_insurances: str = "Obras Sociales"
    sheet_tab_products: str = "productos"
