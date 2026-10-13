"""Scoped API token generations: snapshots, rotation, revocation, lifecycle."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import requests
import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from skyportal.models import (
    DBSession,
    Group,
    GroupUser,
    Obj,
    Source,
    Stream,
    StreamUser,
    Token,
    User,
)
from skyportal.tests import api, cfg
from skyportal.tests.fixtures import ObjFactory


def _expires(days=30):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def create_scoped(
    super_admin_token,
    user,
    groups,
    streams=None,
    acls=("Comment",),
    expires_at=_expires(),
    overlap_seconds=None,
):
    data = {
        "name": uuid.uuid4().hex,
        "acls": list(acls),
        "user_id": user.id,
        "group_ids": [group.id for group in groups],
        "stream_ids": [stream.id for stream in (streams or [])],
        "expires_at": expires_at,
    }
    if overlap_seconds is not None:
        data["overlap_seconds"] = overlap_seconds
    return api("POST", "internal/tokens", token=super_admin_token, data=data)


def _loaded_user(user_id):
    return DBSession().scalar(
        sa.select(User)
        .where(User.id == user_id)
        .options(
            selectinload(User.groups),
            selectinload(User.streams),
            selectinload(User.acls),
        )
    )


def _add_membership(user, *, group=None, stream=None, acl=None):
    if group is not None:
        DBSession().add(GroupUser(user_id=user.id, group_id=group.id))
    if stream is not None:
        DBSession().add(StreamUser(user_id=user.id, stream_id=stream.id))
    if acl is not None:
        loaded = _loaded_user(user.id)
        from skyportal.models import ACL

        loaded.acls.append(DBSession().get(ACL, acl))
    DBSession().commit()


def test_create_scoped_token_and_list_metadata(
    super_admin_token, user, public_group, public_stream
):
    status, data = create_scoped(
        super_admin_token, user, [public_group], [public_stream]
    )
    assert status == 200, data
    secret = data["data"]["token_id"]
    series_id = data["data"]["series_id"]
    assert data["data"]["generation"] == 1
    assert uuid.UUID(series_id)

    # the secret works immediately
    status, data = api("GET", "internal/profile", token=secret)
    assert status == 200

    # profile/list expose metadata but never the full secret
    status, data = api("GET", "internal/profile", token=secret)
    assert status == 200
    tokens = data["data"]["tokens"]
    entry = next(t for t in tokens if t["series_id"] == series_id)
    assert "id" not in entry
    assert entry["fingerprint"] == secret[:8]
    assert entry["status"] == "active"
    assert entry["generation"] == 1
    assert entry["expires_at"] is not None
    assert [g["id"] for g in entry["groups"]] == [public_group.id]
    assert [s["id"] for s in entry["streams"]] == [public_stream.id]
    assert "Comment" in entry["acls"]
    import json as _json

    assert secret not in _json.dumps(tokens)


def test_create_rejects_scope_beyond_owner(
    super_admin_token, user, public_group, public_stream
):
    base = {
        "name": uuid.uuid4().hex,
        "user_id": user.id,
        "group_ids": [public_group.id],
        "stream_ids": [public_stream.id],
        "expires_at": _expires(),
    }

    def post(acls=("Comment",), **overrides):
        payload = {**base, "acls": list(acls), **overrides}
        return api("POST", "internal/tokens", token=super_admin_token, data=payload)

    assert post(acls=["System admin"])[0] == 400
    assert post(acls=["Become user"])[0] == 400
    assert post(group_ids=[999999])[0] == 400
    assert post(stream_ids=[999999])[0] == 400
    assert post(group_ids=[])[0] == 400

    payload = {k: v for k, v in base.items() if k != "expires_at"}
    payload["acls"] = ["Comment"]
    status, data = api("POST", "internal/tokens", token=super_admin_token, data=payload)
    assert status == 400, data


def test_snapshot_does_not_expand_with_owner_growth(
    super_admin_token,
    user,
    public_group,
    public_group2,
    public_stream,
    public_stream2,
):
    status, data = create_scoped(
        super_admin_token,
        user,
        [public_group],
        [public_stream],
        acls=("Comment", "Annotate"),
    )
    assert status == 200, data
    secret = data["data"]["token_id"]

    # owner gains a group, a stream and an ACL after issuance
    _add_membership(user, group=public_group2, stream=public_stream2, acl="Classify")

    # streams visible to the token remain exactly the snapshot
    status, data = api("GET", "streams", token=secret)
    assert status == 200
    stream_ids = {row["id"] for row in data["data"]}
    assert public_stream.id in stream_ids
    assert public_stream2.id not in stream_ids

    # group data beyond the snapshot stays invisible
    obj1 = ObjFactory(groups=[public_group])
    obj2 = ObjFactory(groups=[public_group2])
    DBSession().add_all(
        [
            Source(obj_id=obj1.id, group_id=public_group.id),
            Source(obj_id=obj2.id, group_id=public_group2.id),
        ]
    )
    DBSession().commit()
    status, data = api("GET", "sources", token=secret)
    assert status == 200
    visible = {row["id"] for row in data["data"]["sources"]}
    assert obj1.id in visible
    assert obj2.id not in visible


def test_rotation_overlap_window(super_admin_token, user, public_group):
    status, data = create_scoped(
        super_admin_token, user, [public_group], [], overlap_seconds=60
    )
    assert status == 200, data
    old_secret = data["data"]["token_id"]
    series_id = data["data"]["series_id"]

    status, data = api(
        "POST",
        f"internal/tokens/{old_secret}/rotate",
        token=super_admin_token,
        data={"overlap_seconds": 2},
    )
    assert status == 200, data
    new_secret = data["data"]["token_id"]
    assert new_secret != old_secret
    assert data["data"]["series_id"] == series_id
    assert data["data"]["generation"] == 2

    # during the overlap both generations establish requests
    assert api("GET", "internal/profile", token=old_secret)[0] == 200
    assert api("GET", "internal/profile", token=new_secret)[0] == 200

    # after the window the old generation is dead, the new one alive
    import time

    time.sleep(3)
    assert api("GET", "internal/profile", token=old_secret)[0] == 401
    assert api("GET", "internal/profile", token=new_secret)[0] == 200

    status, data = api("GET", "internal/tokens", token=super_admin_token)
    gens = {t["generation"]: t for t in data["data"] if t["series_id"] == series_id}
    assert gens[1]["status"] == "superseded"
    assert gens[2]["status"] == "active"


def test_expired_generation_rejected(super_admin_token, user, public_group):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    secret = data["data"]["token_id"]
    assert api("GET", "internal/profile", token=secret)[0] == 200

    token = DBSession().get(Token, secret)
    token.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    DBSession().commit()
    assert api("GET", "internal/profile", token=secret)[0] == 401


def test_revoked_generation_and_series_rejected(super_admin_token, user, public_group):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    secret = data["data"]["token_id"]
    series_id = data["data"]["series_id"]

    status, _ = api("POST", f"internal/tokens/{secret}/revoke", token=super_admin_token)
    assert status == 200
    assert api("GET", "internal/profile", token=secret)[0] == 401

    # series revoke retires every generation, including the newest
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    secret2 = data["data"]["token_id"]
    series2 = data["data"]["series_id"]
    status, data = api(
        "POST",
        f"internal/tokens/{secret2}/rotate",
        token=super_admin_token,
        data={"overlap_seconds": 0},
    )
    secret3 = data["data"]["token_id"]
    status, _ = api(
        "POST", f"internal/token-series/{series2}/revoke", token=super_admin_token
    )
    assert status == 200
    assert api("GET", "internal/profile", token=secret2)[0] == 401
    assert api("GET", "internal/profile", token=secret3)[0] == 401
    assert series_id  # series of the first, revoked gen


def test_owner_deactivation_rejects(super_admin_token, user, public_group):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    secret = data["data"]["token_id"]

    loaded = DBSession().get(User, user.id)
    loaded.expiration_date = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    DBSession().commit()
    # scoped generations report an inactive owner as an invalid credential
    assert api("GET", "internal/profile", token=secret)[0] == 401
    # legacy tokens keep the historical 403 contract
    status, data = api(
        "POST",
        "internal/tokens",
        token=super_admin_token,
        data={"name": uuid.uuid4().hex, "acls": [], "user_id": user.id},
    )
    assert status == 200, data
    assert api("GET", "internal/profile", token=data["data"]["token_id"])[0] == 403


def test_permission_source_removal_rejects(
    super_admin_token, user, public_group, public_stream
):
    status, data = create_scoped(
        super_admin_token, user, [public_group], [public_stream]
    )
    secret = data["data"]["token_id"]
    assert api("GET", "internal/profile", token=secret)[0] == 200

    # owner loses the snapshot group membership
    DBSession().query(GroupUser).filter(
        GroupUser.user_id == user.id, GroupUser.group_id == public_group.id
    ).delete()
    DBSession().commit()
    assert api("GET", "internal/profile", token=secret)[0] == 403


def test_legacy_token_keeps_dynamic_inheritance(
    super_admin_token,
    user,
    public_group2,
    public_stream2,
):
    name = uuid.uuid4().hex
    status, data = api(
        "POST",
        "internal/tokens",
        token=super_admin_token,
        data={"name": name, "acls": ["Comment"], "user_id": user.id},
    )
    assert status == 200, data
    legacy_secret = data["data"]["token_id"]

    status, data = api("GET", "streams", token=legacy_secret)
    before = {row["id"] for row in data["data"]}
    assert public_stream2.id not in before

    _add_membership(user, group=public_group2, stream=public_stream2)

    status, data = api("GET", "streams", token=legacy_secret)
    after = {row["id"] for row in data["data"]}
    assert public_stream2.id in after


def test_scoped_token_snapshot_is_immutable(super_admin_token, user, public_group):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    secret = data["data"]["token_id"]
    status, data = api(
        "PUT",
        f"internal/tokens/{secret}",
        token=super_admin_token,
        data={"acls": ["Comment", "Annotate"]},
    )
    assert status == 400


def test_socket_auth_token_exchange(
    super_admin_token, user, public_group, public_stream
):
    status, data = create_scoped(
        super_admin_token, user, [public_group], [public_stream]
    )
    secret = data["data"]["token_id"]

    before = datetime.now(UTC).timestamp()
    response = requests.get(
        f"http://localhost:{cfg['ports.app']}/baselayer/socket_auth_token",
        headers={"Authorization": f"token {secret}"},
        timeout=10,
    )
    assert response.status_code == 200
    payload = jwt.decode(
        response.json()["data"]["token"],
        cfg["app"]["secret_key"],
        algorithms=["HS256"],
    )
    assert payload["user_id"] == str(user.id)
    assert 0 < payload["exp"] - before <= 65

    # revoked generation cannot establish a new connection
    api("POST", f"internal/tokens/{secret}/revoke", token=super_admin_token)
    response = requests.get(
        f"http://localhost:{cfg['ports.app']}/baselayer/socket_auth_token",
        headers={"Authorization": f"token {secret}"},
        timeout=10,
    )
    assert response.status_code == 401


def test_series_and_generation_admin_endpoints(super_admin_token, user, public_group):
    # endpoints addressed without the secret: series id + generation number
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    series_id = data["data"]["series_id"]

    status, data = api(
        "POST",
        f"internal/token-series/{series_id}/rotate",
        token=super_admin_token,
        data={"overlap_seconds": 0},
    )
    assert status == 200, data
    gen2_secret = data["data"]["token_id"]
    assert data["data"]["generation"] == 2

    status, _ = api(
        "POST",
        f"internal/token-series/{series_id}/generations/1",
        token=super_admin_token,
    )
    assert status == 200
    # gen1 may already be superseded; revoke is idempotent-ish and gen2 works
    assert api("GET", "internal/profile", token=gen2_secret)[0] == 200

    status, data = api("GET", "internal/profile", token=gen2_secret)
    assert status == 200

    # delete a single generation row
    status, _ = api(
        "DELETE",
        f"internal/token-series/{series_id}/generations/1",
        token=super_admin_token,
    )
    assert status == 200


def test_legacy_token_management_by_fingerprint(super_admin_token, user, public_group):
    name = uuid.uuid4().hex
    status, data = api(
        "POST",
        "internal/tokens",
        token=super_admin_token,
        data={"name": name, "acls": ["Comment"], "user_id": user.id},
    )
    assert status == 200, data
    secret = data["data"]["token_id"]
    fingerprint = secret[:8]

    status, data = api(
        "PUT",
        f"internal/legacy-tokens/{fingerprint}",
        token=super_admin_token,
        data={"acls": ["Comment", "Annotate"]},
    )
    assert status == 200, data

    status, data = api("GET", "internal/profile", token=secret)
    assert status == 200
    assert set(data["data"]["tokens"][0]["acls"]) == {"Comment", "Annotate"}

    status, _ = api(
        "DELETE", f"internal/legacy-tokens/{fingerprint}", token=super_admin_token
    )
    assert status == 200
    assert api("GET", "internal/profile", token=secret)[0] == 401


def test_concurrent_rotations_converge(super_admin_token, user, public_group):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    secret = data["data"]["token_id"]
    series_id = data["data"]["series_id"]

    results, failures = [], []

    def rotate():
        try:
            status, data = api(
                "POST",
                f"internal/tokens/{secret}/rotate",
                token=super_admin_token,
                data={"overlap_seconds": 0},
            )
            if status == 200:
                results.append(data["data"])
            else:
                failures.append((status, data))
        except Exception as e:  # pragma: no cover - diagnostics
            failures.append(("exc", repr(e)))

    threads = [threading.Thread(target=rotate) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, failures
    assert sorted(r["generation"] for r in results) == list(range(2, 12))
    assert len({r["token_id"] for r in results}) == 10

    # exactly the latest generation is usable in a fresh process' view
    generations = (
        DBSession().scalars(sa.select(Token).where(Token.series_id == series_id)).all()
    )
    assert sorted(g.generation for g in generations) == list(range(1, 12))

    newest = max(generations, key=lambda g: g.generation).id
    assert api("GET", "internal/profile", token=newest)[0] == 200
    older = [g for g in generations if g.id != newest]
    assert all(api("GET", "internal/profile", token=g.id)[0] == 401 for g in older)


def test_non_owner_cannot_rotate_or_revoke(
    super_admin_token, user, user2, public_group, view_only_token2
):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    assert status == 200, data
    secret = data["data"]["token_id"]

    # an unrelated authenticated user can neither rotate nor revoke by secret
    status, _ = api(
        "POST",
        f"internal/tokens/{secret}/rotate",
        token=view_only_token2,
        data={"overlap_seconds": 0},
    )
    assert status == 400
    status, _ = api(
        "POST", f"internal/tokens/{secret}/revoke", token=view_only_token2
    )
    assert status == 400

    # the owner's generation is untouched and still establishes requests
    assert api("GET", "internal/profile", token=secret)[0] == 200


def test_non_admin_cannot_delegate_token_creation(
    user, user2, public_group, view_only_token2
):
    scoped_name, legacy_name = uuid.uuid4().hex, uuid.uuid4().hex

    # scoped creation for another user is admin-only
    status, data = api(
        "POST",
        "internal/tokens",
        token=view_only_token2,
        data={
            "name": scoped_name,
            "acls": [],
            "user_id": user.id,
            "group_ids": [public_group.id],
            "expires_at": _expires(),
        },
    )
    assert status == 400, data

    # the same holds for the legacy path
    status, data = api(
        "POST",
        "internal/tokens",
        token=view_only_token2,
        data={"name": legacy_name, "acls": [], "user_id": user.id},
    )
    assert status == 400, data

    # neither attempt left a token behind
    leftover = DBSession().scalars(
        sa.select(Token).where(Token.name.in_([scoped_name, legacy_name]))
    ).all()
    assert leftover == []


def test_token_can_rotate_itself(super_admin_token, user, public_group):
    status, data = create_scoped(super_admin_token, user, [public_group], [])
    assert status == 200, data
    secret, series_id = data["data"]["token_id"], data["data"]["series_id"]

    # an integration holding the secret rotates it without a browser session
    status, data = api(
        "POST",
        f"internal/tokens/{secret}/rotate",
        token=secret,
        data={"overlap_seconds": 0},
    )
    assert status == 200, data
    assert data["data"]["series_id"] == series_id
    assert data["data"]["generation"] == 2
    assert api("GET", "internal/profile", token=data["data"]["token_id"])[0] == 200


def test_legacy_metadata_lists_inherited_scope(
    super_admin_token, user, public_group
):
    name = uuid.uuid4().hex
    status, data = api(
        "POST",
        "internal/tokens",
        token=super_admin_token,
        data={"name": name, "acls": [], "user_id": user.id},
    )
    assert status == 200, data

    status, data = api("GET", "internal/tokens", token=super_admin_token)
    assert status == 200
    entry = next(
        t for t in data["data"] if t["name"] == name and not t["series_id"]
    )
    # legacy tokens display the owner's currently inherited groups
    group_ids = {g["id"] for g in entry["groups"]}
    assert public_group.id in group_ids
    assert entry["status"] == "active"
