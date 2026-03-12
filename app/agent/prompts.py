from app.clients.models import ClientConfig

def build_system_prompt(config: ClientConfig) -> str:
    return config.system_prompt