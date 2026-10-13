"""Response models for ``/api/internal/tokens``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiTokenResponse(BaseModel):
    """An API token generation (baselayer ``Token``).

    The full secret is never included here; ``fingerprint`` is a short,
    non-secret prefix. ``acls``/``groups``/``streams`` describe the snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    created_at: datetime | None = None
    modified: datetime | None = None
    created_by_id: int | None = None
    name: str | None = None
    acls: list[str] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    streams: list[dict[str, Any]] = Field(default_factory=list)
    series_id: str | None = None
    generation: int = 1
    expires_at: datetime | None = None
    retire_at: datetime | None = None
    revoked_at: datetime | None = None
    status: str = "active"


class TokenPostBody(BaseModel):
    """Request body for creating a new API token.

    With ``group_ids``/``expires_at`` set, a scoped generation is created;
    otherwise the legacy dynamic-inheritance token is created.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Name of the token")
    acls: list[str] = Field(description="List of ACL IDs to grant the token")
    user_id: int | None = Field(
        default=None,
        description="ID of the user to create the token for; defaults to the requesting user",
    )
    group_ids: list[int] | None = Field(
        default=None,
        description="Scoped snapshot groups; presence makes the token scoped and requires at least one",
    )
    stream_ids: list[int] | None = Field(
        default=None,
        description="Scoped snapshot streams; may be an empty list for scoped tokens",
    )
    expires_at: datetime | None = Field(
        default=None, description="UTC expiry; required for scoped tokens"
    )
    overlap_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
        description="Default rotation overlap window in seconds (0-3600, default 60)",
    )


class TokenSecretResponse(BaseModel):
    """The new secret, returned exactly once on create/rotate."""

    token_id: str = Field(description="Token secret (bearer value)")
    series_id: str | None = Field(default=None, description="Series ID")
    generation: int = 1


class TokenPostResponse(TokenSecretResponse):
    """ID of the newly created token."""


class TokenPutBody(BaseModel):
    """Request body for updating a legacy token (scoped snapshots are immutable)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="New name of the token")
    acls: list[str] | None = Field(
        default=None, description="New list of ACL IDs for the token"
    )
    user_id: int | None = Field(
        default=None,
        description="ID of the user whose permissions the new ACLs are checked against; defaults to the requesting user",
    )


class TokenRotateBody(BaseModel):
    """Request body for rotating a token generation within its series."""

    model_config = ConfigDict(extra="forbid")

    overlap_seconds: int | None = Field(
        default=None, ge=0, le=3600, description="Overlap window (0-3600)"
    )
    expires_at: datetime | None = Field(
        default=None, description="UTC expiry for the new generation"
    )


__all__ = [
    "ApiTokenResponse",
    "TokenPostBody",
    "TokenPostResponse",
    "TokenPutBody",
    "TokenRotateBody",
    "TokenSecretResponse",
]
