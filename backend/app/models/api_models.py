from pydantic import BaseModel


class InitRequest(BaseModel):
    client_id: str


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


class UploadResponse(BaseModel):
    image_url: str
    file_size: int


class SubmitTicketRequest(BaseModel):
    session_id: str


class SubmitTicketResponse(BaseModel):
    success: bool
    ticket_id: str
    message: str | None = None
