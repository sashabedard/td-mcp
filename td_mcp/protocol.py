from typing import Any, Literal

from pydantic import BaseModel, Field


class BridgeRequest(BaseModel):
    id: int
    action: str
    data: dict[str, Any] = Field(default_factory=dict)
    token: str | None = None


class BridgeErrorBody(BaseModel):
    message: str
    traceback: str | None = None


class BridgeResponse(BaseModel):
    id: int | None
    ok: bool
    result: dict[str, Any] | None = None
    error: BridgeErrorBody | None = None


class TDError(RuntimeError):
    """Error returned by the TouchDesigner side of the bridge."""

    def __init__(self, message: str, traceback: str | None = None):
        super().__init__(message)
        self.message = message
        self.traceback = traceback


OpFamily = Literal["CHOP", "TOP", "SOP", "DAT", "COMP", "MAT", "POP"]
