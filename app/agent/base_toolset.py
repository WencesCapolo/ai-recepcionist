from typing import Protocol, Any
from app.clients.models import ClientConfig

class ToolsetProvider(Protocol):
    name: str
    required_tools: frozenset[str]

    def is_applicable(self, config: ClientConfig) -> bool: ...
    def build(self, config: ClientConfig, **deps: Any) -> list[dict]: ...
