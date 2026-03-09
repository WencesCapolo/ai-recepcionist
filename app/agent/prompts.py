from app.clients.models import ClientConfig

def build_system_prompt(config: ClientConfig) -> str:
    """
    Builds the complete system prompt for the LangGraph agent.
    Combines the operator-defined prompt with strict behavioral rules.
    """
    behavior_rules = """
# --- END OF OPERATOR CONFIGURATION ---
# --- BEGIN SYSTEM BEHAVIOR RULES ---

You must strictly adhere to the following rules at all times:
1. Always respond in Spanish.
2. Never invent prices, stock, or hours — always call a tool first to verify information.
3. Keep responses concise and WhatsApp-friendly (do not use markdown headers, do not write long paragraphs).
4. Never mention that you are an AI, an assistant, or a bot unless directly asked.
5. If a tool returns no result or an error, apologize briefly to the user and suggest calling the store directly.
"""
    
    return f"{config.system_prompt.strip()}\n{behavior_rules}"
