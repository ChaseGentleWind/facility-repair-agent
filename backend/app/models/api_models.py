from pydantic import BaseModel, Field


class InitRequest(BaseModel):
    client_id: str
    metadata: dict = Field(default_factory=dict)


class InitResponse(BaseModel):
    session_id: str
    greeting: str
    expires_in: int


class ChatMessage(BaseModel):
    type: str = "text"          # "text" | "image_url"
    content: str
    image_url: str | None = None


class MessageRequest(BaseModel):
    session_id: str
    message: ChatMessage
