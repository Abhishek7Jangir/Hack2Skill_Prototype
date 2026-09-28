"""Request models (pydantic v2). Responses are plain dicts built in the services."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_str(v):
    """Accept 342007 as well as "342007" for codes; lgd codes stay opaque strings."""
    if v is None:
        return None
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, str):
        return v.strip() or None
    return v


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    method: Literal["pincode", "manual", "gps"]
    pincode: str | None = None
    lgd_code: str | None = Field(None, description="Village lgd_code (manual: required; pincode: the picked village)")
    district_lgd_code: str | None = Field(None, description="Pincode + 'Not listed': the chosen district")
    lat: float | None = None
    lng: float | None = None

    @field_validator("pincode", "lgd_code", "district_lgd_code", mode="before")
    @classmethod
    def _codes(cls, v):
        return _to_str(v)


class ComplaintIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_type: Literal["voice", "text"]
    text: str | None = Field(None, max_length=5000)
    audio_file: str | None = Field(None, description="base64 audio (Flow A voice). Not stored.")
    category: Literal["water", "roads"]
    severity: int | None = Field(None, ge=1, le=5, description="Present (with urgency) => Flow B")
    urgency: Literal["low", "medium", "high"] | None = None
    location: LocationIn
    language: str | None = Field(None, max_length=10)
    channel: Literal["web", "voice", "messaging"] = "web"
    timestamp: datetime | None = Field(None, description="Client-side time, stored as client_timestamp")

    @field_validator("urgency", mode="before")
    @classmethod
    def _lower_urgency(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class StatusUpdateIn(BaseModel):
    status: Literal["open", "in_progress", "resolved", "rejected"]
    updated_by: str = Field(..., min_length=1, max_length=100)
    note: str | None = Field(None, max_length=2000)


class MockProcessIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    input_type: str | None = None
    text: str | None = None
    audio_file: str | None = None
    language: str | None = None


class MockNarrateIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    evidence: list = []
    sample_descriptions: list = []
