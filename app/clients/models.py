# app/clients/models.py
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from uuid import UUID

from app.agent.registry import KNOWN_TOOL_NAMES


class RetailToolConfig(BaseModel):
    sheet_id: str
    tab: str = "productos"
    columns: dict[str, str] = Field(
        default_factory=lambda: {
            "product": "producto",
            "category": "categoria",
            "price": "precio",
            "stock": "stock",
            "unit": "unidad",
        }
    )


class CalendarToolConfig(BaseModel):
    calendar_id: Optional[str] = None  # None → use CalendarMock
    slot_minutes: int = 30
    work_start: int = 10
    work_end: int = 18
    work_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    timezone: str = "America/Argentina/Cordoba"


class DentistSheetsConfig(BaseModel):
    sheet_id: str
    tab_treatments: str = "Tratamientos"
    tab_insurances: str = "Obras Sociales"


class PadelConfig(BaseModel):
    calendar_id: str
    slot_minutes: int = 60


class PaymentConfig(BaseModel):
    access_token: str
    sandbox: bool = True


class ResellerConfig(BaseModel):
    sheet_id: str
    tab: str = "revendedores"
    columns: dict[str, str] = Field(
        default_factory=lambda: {
            "localidad": "localidad",
            "provincia": "provincia",
            "nombre": "nombre_local",
            "contacto": "contacto",
            "direccion": "direccion",
        }
    )


class ToolConfig(BaseModel):
    retail: Optional[RetailToolConfig] = None
    calendar: Optional[CalendarToolConfig] = None
    dentist_sheets: Optional[DentistSheetsConfig] = None
    padel: Optional[PadelConfig] = None
    payment: Optional[PaymentConfig] = None
    reseller: Optional[ResellerConfig] = None


class ClientConfig(BaseModel):
    id: UUID
    name: str
    whatsapp_number: str
    system_prompt: str
    tools_enabled: list[str] = Field(default_factory=list)
    prompt_version: int
    active: bool
    tool_config: ToolConfig = Field(default_factory=ToolConfig)

    @field_validator("tools_enabled")
    @classmethod
    def validate_tool_names(cls, v: list[str]) -> list[str]:
        unknown = set(v) - KNOWN_TOOL_NAMES
        if unknown:
            raise ValueError(f"Unknown tool names: {unknown}")
        return v
