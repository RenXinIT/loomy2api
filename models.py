from typing import Any
from pydantic import BaseModel, ConfigDict


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    # OpenAI-compatible optional stable user identifier. Used only as a
    # conversation-isolation fallback when no explicit session header exists.
    user: str | None = None
