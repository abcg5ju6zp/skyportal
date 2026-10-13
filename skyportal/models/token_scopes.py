"""Scoped API token generations: scope validation, lifecycle status, rotation.

All state transitions run in a single transaction and take a row lock on the
``token_series`` row first, so concurrent rotation/revocation serializes onto
the durable database state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from baselayer.app.models import ACL, DBSession, Token, TokenSeries, User

from .user_token import GroupToken, StreamToken

DEFAULT_OVERLAP_SECONDS = 60
MAX_OVERLAP_SECONDS = 3600

ACTIVE = "active"
OVERLAPPING = "overlapping"
EXPIRED = "expired"
REVOKED = "revoked"
SUPERSEDED = "superseded"
OWNER_DISABLED = "owner_disabled"
SOURCE_REMOVED = "source_removed"

USABLE_STATUSES = (ACTIVE, OVERLAPPING)


class TokenScopeError(ValueError):
    """A requested scope exceeds the owner's current permissions."""


class TokenLifecycleError(ValueError):
    """An operation conflicts with the generation's lifecycle state."""


def _now():
    # Timestamps are naive UTC throughout the schema.
    return datetime.now(UTC).replace(tzinfo=None)


def as_naive_utc(value):
    """Normalize a possibly timezone-aware datetime to naive UTC."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def clamp_overlap_seconds(overlap_seconds):
    if overlap_seconds is None:
        return DEFAULT_OVERLAP_SECONDS
    try:
        overlap = int(overlap_seconds)
    except (TypeError, ValueError) as e:
        raise TokenScopeError("overlap_seconds must be an integer.") from e
    if overlap < 0:
        return 0
    return min(overlap, MAX_OVERLAP_SECONDS)


def _owner_loads():
    return (
        selectinload(User.acls),
        selectinload(User.roles),
        selectinload(User.groups),
        selectinload(User.streams),
    )


def _generation_loads():
    return (
        selectinload(Token.acls),
        selectinload(Token.scoped_groups),
        selectinload(Token.scoped_streams),
        selectinload(Token.created_by).options(*_owner_loads()),
    )


def load_owner(session, user_id):
    return session.scalar(
        sa.select(User).where(User.id == user_id).options(*_owner_loads())
    )


def load_generation(session, token_id):
    """Load a token with its snapshot and fully-loaded owner."""
    return session.scalar(
        sa.select(Token).where(Token.id == token_id).options(*_generation_loads())
    )


def validate_scopes(owner, acls, group_ids, stream_ids):
    """Validate that a snapshot is a subset of the owner's current permissions.

    Returns the normalized ``(acls, group_ids, stream_ids)``; raises
    ``TokenScopeError`` otherwise.
    """
    acls = list(acls or [])
    group_ids = {int(gid) for gid in (group_ids or [])}
    stream_ids = {int(sid) for sid in (stream_ids or [])}

    if "System admin" in acls:
        raise TokenScopeError("Scoped tokens may not carry the 'System admin' ACL.")

    permitted_acls = set(owner.permissions)
    forbidden_acls = set(acls) - permitted_acls
    if forbidden_acls:
        raise TokenScopeError(
            "Token ACLs must be a subset of the owner's permissions: "
            f"{sorted(forbidden_acls)}."
        )

    if not group_ids:
        raise TokenScopeError("A scoped token requires at least one group.")
    permitted_groups = {group.id for group in owner.groups}
    forbidden_groups = group_ids - permitted_groups
    if forbidden_groups:
        raise TokenScopeError(
            f"Token groups must be a subset of the owner's groups: "
            f"{sorted(forbidden_groups)}."
        )

    permitted_streams = {stream.id for stream in owner.streams}
    forbidden_streams = stream_ids - permitted_streams
    if forbidden_streams:
        raise TokenScopeError(
            "Token streams must be a subset of the owner's streams: "
            f"{sorted(forbidden_streams)}."
        )

    return acls, sorted(group_ids), sorted(stream_ids)


def snapshot_is_current(token, owner):
    """Whether every snapshot source is still held by the owner."""
    if {acl.id for acl in token.acls} - set(owner.permissions):
        return False
    if {group.id for group in token.scoped_groups} - {
        group.id for group in owner.groups
    }:
        return False
    if {stream.id for stream in token.scoped_streams} - {
        stream.id for stream in owner.streams
    }:
        return False
    return True


def token_generation_status(token, now=None, owner=None):
    """Derive the display/availability status of a generation.

    Legacy tokens (no series) always report ``active``; they remain on their
    inherited-compatibility path. A pre-loaded ``owner`` avoids lazy-loading
    the relationship inside an async session.
    """
    if not token.is_scoped:
        return ACTIVE
    now = now or _now()
    if owner is None:
        owner = token.created_by
    if owner is None or not owner.is_active():
        return OWNER_DISABLED
    if token.revoked_at is not None:
        return REVOKED
    if token.expires_at is not None and now >= token.expires_at:
        return EXPIRED
    if token.retire_at is not None and now >= token.retire_at:
        return SUPERSEDED
    if not snapshot_is_current(token, owner):
        return SOURCE_REMOVED
    if token.retire_at is not None:
        return OVERLAPPING
    return ACTIVE


def can_establish_new_requests(token, now=None):
    return token_generation_status(token, now=now) in USABLE_STATUSES


def generation_metadata(token, owner=None):
    """Serialize a generation for list/profile responses.

    Never includes the full secret: only an 8-character fingerprint. A
    pre-loaded ``owner`` may be supplied (e.g. the profile's own user) to
    avoid lazy-loading ``token.created_by``. Legacy tokens display the
    owner's current groups/streams, matching their dynamic inheritance.
    """
    if owner is None:
        owner = token.created_by
    return {
        "fingerprint": token.id[:8],
        "name": token.name,
        "created_at": token.created_at,
        "modified": token.modified,
        "created_by_id": token.created_by_id,
        "acls": sorted(acl.id for acl in token.acls),
        "groups": [
            {"id": group.id, "name": group.name}
            for group in (
                token.scoped_groups
                if token.is_scoped
                else getattr(owner, "groups", [])
            )
        ],
        "streams": [
            {"id": stream.id, "name": stream.name}
            for stream in (
                token.scoped_streams
                if token.is_scoped
                else getattr(owner, "streams", [])
            )
        ],
        "series_id": token.series_id,
        "generation": token.generation,
        "expires_at": token.expires_at,
        "retire_at": token.retire_at,
        "revoked_at": token.revoked_at,
        "status": token_generation_status(token, owner=owner),
    }


def _acl_rows(session, acls):
    if not acls:
        return []
    return list(session.scalars(sa.select(ACL).where(ACL.id.in_(acls))).unique().all())


def _copy_snapshot(session, new_token, source_token):
    new_token.acls = list(source_token.acls)
    session.flush()
    session.add_all(
        GroupToken(token_id=new_token.id, group_id=group.id)
        for group in source_token.scoped_groups
    )
    session.add_all(
        StreamToken(token_id=new_token.id, stream_id=stream.id)
        for stream in source_token.scoped_streams
    )


def create_scoped_token(
    user_id, name, acls, group_ids, stream_ids, expires_at, overlap_seconds=None
):
    """Create a series and its generation 1 with an immutable snapshot."""
    expires_at = as_naive_utc(expires_at)
    if expires_at is None:
        raise TokenScopeError("A scoped token requires an expiry time.")
    overlap = clamp_overlap_seconds(overlap_seconds)

    with DBSession() as session:
        owner = load_owner(session, user_id)
        if owner is None:
            raise TokenScopeError(f"User {user_id} not found.")
        acls, group_ids, stream_ids = validate_scopes(
            owner, acls, group_ids, stream_ids
        )
        if expires_at <= _now():
            raise TokenScopeError("expires_at must be in the future.")

        series = TokenSeries(name=name, created_by=owner, overlap_seconds=overlap)
        token = Token(
            name=name,
            created_by=owner,
            series=series,
            generation=1,
            expires_at=expires_at,
        )
        token.acls = _acl_rows(session, acls)
        session.add(series)
        session.add(token)
        session.flush()
        session.add_all(
            GroupToken(token_id=token.id, group_id=gid) for gid in group_ids
        )
        session.add_all(
            StreamToken(token_id=token.id, stream_id=sid) for sid in stream_ids
        )
        session.commit()
        return {
            "token_id": token.id,
            "series_id": series.id,
            "generation": 1,
        }


def _locked_series(session, series_id):
    return session.scalar(
        sa.select(TokenSeries).where(TokenSeries.id == series_id).with_for_update()
    )


def rotate_token(token_id, overlap_seconds=None, expires_at=None):
    """Add a generation to the token's series.

    The previous latest generation receives ``retire_at = now + overlap``;
    snapshot and ACLs are copied from the latest generation and re-validated
    against the owner's current permissions.
    """
    with DBSession() as session:
        anchor = session.get(Token, token_id)
        if anchor is None:
            raise TokenLifecycleError(f"Token {token_id} not found.")
        if anchor.series_id is None:
            raise TokenLifecycleError("Legacy tokens cannot be rotated.")
        series = _locked_series(session, anchor.series_id)
        return _rotate_locked(
            session, series, overlap_seconds, as_naive_utc(expires_at)
        )


def rotate_series(series_id, overlap_seconds=None, expires_at=None):
    """Add a generation to a series identified by its (non-secret) id."""
    expires_at = as_naive_utc(expires_at)
    with DBSession() as session:
        series = _locked_series(session, series_id)
        if series is None:
            raise TokenLifecycleError(f"Token series {series_id} not found.")
        return _rotate_locked(session, series, overlap_seconds, expires_at)


def _rotate_locked(session, series, overlap_seconds, expires_at):
    generations = (
        session.scalars(
            sa.select(Token)
            .where(Token.series_id == series.id)
            .options(*_generation_loads())
        )
        .unique()
        .all()
    )
    latest = max(generations, key=lambda t: t.generation)
    if latest.revoked_at is not None:
        raise TokenLifecycleError(
            "The latest generation is revoked; create a new token series."
        )

    owner = latest.created_by
    if owner is None or not owner.is_active():
        raise TokenLifecycleError("The token owner is not active.")
    if not snapshot_is_current(latest, owner):
        raise TokenScopeError(
            "The token snapshot is no longer a subset of the owner's "
            "permissions; create a new token with the desired scope."
        )

    overlap = (
        clamp_overlap_seconds(overlap_seconds)
        if overlap_seconds is not None
        else series.overlap_seconds
    )
    new_expires_at = as_naive_utc(expires_at) or latest.expires_at
    if new_expires_at is not None and new_expires_at <= _now():
        raise TokenScopeError("expires_at must be in the future.")

    new_token = Token(
        name=series.name,
        created_by=owner,
        series=series,
        generation=latest.generation + 1,
        expires_at=new_expires_at,
    )
    session.add(new_token)
    _copy_snapshot(session, new_token, latest)
    latest.retire_at = _now() + timedelta(seconds=overlap)
    session.commit()
    return {
        "token_id": new_token.id,
        "series_id": series.id,
        "generation": new_token.generation,
    }


def revoke_generation(token_id):
    """Revoke a single generation immediately."""
    with DBSession() as session:
        token = session.get(Token, token_id)
        if token is None:
            raise TokenLifecycleError(f"Token {token_id} not found.")
        if token.series_id is not None:
            _locked_series(session, token.series_id)
        now = _now()
        if token.revoked_at is None:
            token.revoked_at = now
            session.commit()
        return {"token_id": token.id, "series_id": token.series_id}


def revoke_generation_by_ref(series_id, generation):
    """Revoke a generation addressed by (non-secret) series id + number."""
    with DBSession() as session:
        _locked_series(session, series_id)
        token = session.scalar(
            sa.select(Token).where(
                Token.series_id == series_id, Token.generation == int(generation)
            )
        )
        if token is None:
            raise TokenLifecycleError(
                f"Generation {generation} of series {series_id} not found."
            )
        if token.revoked_at is None:
            token.revoked_at = _now()
            session.commit()
        return {"token_id": token.id}


def delete_generation_by_ref(series_id, generation):
    """Delete a generation row addressed by series id + number."""
    with DBSession() as session:
        # Serialize against rotation: deleting the latest generation while a
        # rotation computes max(generation)+1 could otherwise reuse a number.
        _locked_series(session, series_id)
        token = session.scalar(
            sa.select(Token).where(
                Token.series_id == series_id, Token.generation == int(generation)
            )
        )
        if token is None:
            raise TokenLifecycleError(
                f"Generation {generation} of series {series_id} not found."
            )
        token_id = token.id
        session.delete(token)
        session.commit()
        return {"token_id": token_id}


def revoke_series(series_id):
    """Revoke every generation of a series immediately."""
    with DBSession() as session:
        series = _locked_series(session, series_id)
        if series is None:
            raise TokenLifecycleError(f"Token series {series_id} not found.")
        now = _now()
        revoked = []
        for token in series.generations:
            if token.revoked_at is None:
                token.revoked_at = now
            revoked.append(token.id)
        session.commit()
        return {"series_id": series.id, "token_ids": revoked}
