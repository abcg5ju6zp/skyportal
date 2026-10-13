from skyportal_py_models.tokens import (
    TokenPostBody,
    TokenPostResponse,
    TokenPutBody,
    TokenRotateBody,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from baselayer.app.access import auth_or_token

from ....model_util import create_token
from ....models import ACL, Token, TokenSeries, User
from ....models.token_scopes import (
    TokenLifecycleError,
    TokenScopeError,
    create_scoped_token,
    delete_generation_by_ref,
    generation_metadata,
    revoke_generation,
    revoke_generation_by_ref,
    revoke_series,
    rotate_series,
    rotate_token,
)
from ....utils.assistant import SERVICE_TOKEN_PREFIX
from ...base import BaseHandler


def _is_scoped_request(body):
    return body.group_ids is not None or body.expires_at is not None


def _token_list_options():
    # generation_metadata derives status from both the snapshot and the
    # owner's current permissions, so eager-load the full chain.
    return [
        selectinload(Token.acls),
        selectinload(Token.scoped_groups),
        selectinload(Token.scoped_streams),
        selectinload(Token.created_by).options(
            selectinload(User.acls),
            selectinload(User.roles),
            selectinload(User.groups),
            selectinload(User.streams),
        ),
    ]


class TokenHandler(BaseHandler):
    @auth_or_token
    async def post(self, *, body: TokenPostBody = None) -> TokenPostResponse:
        """
        ---
        description: Generate new token (legacy tokens are limited to 1 per user)
        """
        body = self.parse_body(TokenPostBody)

        if body.name.startswith(SERVICE_TOKEN_PREFIX):
            return self.error(
                f'A token name cannot start with "{SERVICE_TOKEN_PREFIX}"'
            )

        async with self.AsyncSession() as session:
            caller = self.associated_user_object
            if body.user_id is not None:
                if body.user_id != caller.id and not caller.is_admin:
                    return self.error(
                        "Only system admins may create tokens for another user."
                    )
                user_id = body.user_id
                target = await session.scalar(
                    User.select(session.user_or_token)
                    .options(
                        selectinload(User.acls),
                        selectinload(User.roles),
                    )
                    .where(User.id == user_id)
                )
                if target is None:
                    return self.error("The specified user could not be found.")
            else:
                user_id = caller.id
                target = caller

            if _is_scoped_request(body):
                return await self._create_scoped(session, user_id, body)

            token_acls = set(body.acls)
            if not all(acl_id in target.permissions for acl_id in token_acls):
                return self.error(
                    "User has attempted to grant token ACLs they do not have "
                    "access to. Please try again."
                )
            existing_result = await session.scalars(
                Token.select(session.user_or_token).where(
                    Token.created_by_id == user_id,
                    Token.series_id.is_(None),
                    Token.name.notlike(f"{SERVICE_TOKEN_PREFIX}%"),
                )
            )
            existing_tokens = existing_result.all()
            if len(existing_tokens) > 0 and not self.associated_user_object.is_admin:
                return self.error(
                    "You have reached the maximum number of tokens "
                    "allowed for your account type."
                )
            token_name = body.name
            existing_name = await session.scalar(
                Token.select(session.user_or_token).where(
                    Token.name == token_name,
                    Token.series_id.is_(None),
                )
            )
            if existing_name:
                return self.error("Duplicate token name.")
            token_id = create_token(ACLs=token_acls, user_id=user_id, name=token_name)
            self.push(
                action="baselayer/SHOW_NOTIFICATION",
                payload={"note": f'Token "{token_name}" created.', "type": "info"},
            )
            return self.success(
                data={"token_id": token_id}, action="skyportal/FETCH_USER_PROFILE"
            )

    async def _create_scoped(self, session, user_id, body):
        try:
            result = create_scoped_token(
                user_id=user_id,
                name=body.name,
                acls=body.acls,
                group_ids=body.group_ids or [],
                stream_ids=body.stream_ids or [],
                expires_at=body.expires_at,
                overlap_seconds=body.overlap_seconds,
            )
        except TokenScopeError as e:
            return self.error(str(e))
        except IntegrityError:
            return self.error("Duplicate token name.")
        self.push(
            action="baselayer/SHOW_NOTIFICATION",
            payload={"note": f'Token "{body.name}" created.', "type": "info"},
        )
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success(data=result)

    @auth_or_token
    async def get(self, token_id: str | None = None):
        """
        ---
        single:
          description: Retrieve a token
          tags:
            - tokens
          responses:
            200:
              content:
                application/json:
                  schema: SingleToken
            400:
              content:
                application/json:
                  schema: Error
        multiple:
          description: Retrieve all tokens
          tags:
            - tokens
          parameters:
            - in: query
              name: userID
              schema:
                type: integer
              description: Filter by user ID
          responses:
            200:
              content:
                application/json:
                  schema: ArrayOfTokens
            400:
              content:
                application/json:
                  schema: Error
        """

        user_id = self.get_query_argument("userID", None, type=int)
        options = _token_list_options()

        async with self.AsyncSession() as session:
            if token_id is not None:
                t = await session.scalar(
                    Token.select(session.user_or_token)
                    .options(*options)
                    .where(Token.id == token_id)
                )
                if t is None:
                    return self.error(f"Could not load token with ID {token_id}")
                return self.success(data=generation_metadata(t))

            stmt = Token.select(session.user_or_token).options(*options)
            if user_id is not None:
                stmt = stmt.where(Token.created_by_id == user_id)
            result = await session.scalars(stmt)
            return self.success(
                data=[generation_metadata(t) for t in result.unique().all()]
            )

    @auth_or_token
    async def put(self, token_id: str, *, body: TokenPutBody = None):
        """
        ---
        description: Update legacy token name/ACLs (scoped snapshots are immutable)
        tags:
          - tokens
        responses:
          200:
            content:
              application/json:
              schema: Success
          400:
            content:
              application/json:
              schema: Error
        """
        body = self.parse_body(TokenPutBody)

        async with self.AsyncSession() as session:
            try:
                token = await session.scalar(
                    Token.select(session.user_or_token, mode="update").where(
                        Token.id == token_id
                    )
                )
                if token is None:
                    return self.error(
                        "Either the specified token does not exist, "
                        "or the user does not have the necessary "
                        "permissions to update it."
                    )

                if token.is_scoped:
                    return self.error(
                        "Scoped token snapshots are immutable; rotate the "
                        "token series to issue a new generation."
                    )

                if body.user_id is not None:
                    user = await session.scalar(
                        User.select(session.user_or_token)
                        .options(
                            selectinload(User.acls),
                            selectinload(User.roles),
                        )
                        .where(User.id == body.user_id)
                    )
                else:
                    user = self.associated_user_object

                if body.name is not None:
                    if body.name.startswith(SERVICE_TOKEN_PREFIX):
                        return self.error(
                            f'A token name cannot start with "{SERVICE_TOKEN_PREFIX}"'
                        )
                    token.name = body.name

                if body.acls is not None:
                    token_acls = set(body.acls)
                    if not all(acl_id in user.permissions for acl_id in token_acls):
                        return self.error(
                            "User has attempted to grant token ACLs they do not have "
                            "access to. Please try again."
                        )

                    new_acl_ids = list(set(token_acls))
                    valid_ids_result = await session.scalars(
                        ACL.select(session.user_or_token, columns=[ACL.id]).where(
                            ACL.id.in_(new_acl_ids)
                        )
                    )
                    valid_ids = set(valid_ids_result.all())
                    if set(new_acl_ids) != valid_ids:
                        return self.error(
                            "Improperly formatted parameter aclIds; must be an array of strings corresponding to valid ACLs."
                        )
                    if len(new_acl_ids) == 0:
                        return self.error(f"No new ACLs to add to token {token_id}")

                    new_acls_result = await session.scalars(
                        ACL.select(session.user_or_token).where(ACL.id.in_(new_acl_ids))
                    )
                    new_acls = new_acls_result.unique().all()

                    token.acls = new_acls
                    session.add(token)

                await session.commit()
                self.push(
                    action="skyportal/FETCH_USER_PROFILE",
                )

                return self.success()
            except Exception as e:
                return self.error(f"Could not update token: {e}")

    @auth_or_token
    async def delete(self, token_id: str):
        """
        ---
        description: Delete a token generation
        responses:
          200:
            content:
              application/json:
              schema: Success
          400:
            content:
              application/json:
              schema: Error
        """

        async with self.AsyncSession() as session:
            token = await session.scalar(
                Token.select(session.user_or_token, mode="delete").where(
                    Token.id == token_id
                )
            )
            if token is None:
                return self.error(
                    "Either the specified token does not exist, "
                    "or the user does not have the necessary "
                    "permissions to delete it."
                )
            token_name = token.name
            await session.delete(token)
            await session.commit()

            self.push(
                action="baselayer/SHOW_NOTIFICATION",
                payload={"note": f'Token "{token_name}" deleted.', "type": "info"},
            )
            return self.success(action="skyportal/FETCH_USER_PROFILE")


class TokenRotateHandler(BaseHandler):
    @auth_or_token
    async def post(self, token_id: str, *, body: TokenRotateBody = None):
        """
        ---
        description: Rotate a scoped token, issuing a new generation in the same series
        tags:
          - tokens
        responses:
          200:
            content:
              application/json:
              schema: Success
          400:
            content:
              application/json:
              schema: Error
        """
        body = self.parse_body(TokenRotateBody)
        async with self.AsyncSession() as session:
            accessible = await session.scalar(
                Token.select(session.user_or_token, mode="update").where(
                    Token.id == token_id
                )
            )
            if accessible is None:
                return self.error(
                    "Token not found, or you do not have permission to rotate it."
                )
        try:
            result = rotate_token(
                token_id,
                overlap_seconds=body.overlap_seconds,
                expires_at=body.expires_at,
            )
        except (TokenScopeError, TokenLifecycleError) as e:
            return self.error(str(e))
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success(data=result)


class TokenRevokeHandler(BaseHandler):
    @auth_or_token
    async def post(self, token_id: str):
        """
        ---
        description: Revoke a single token generation
        tags:
          - tokens
        responses:
          200:
            content:
              application/json:
              schema: Success
          400:
            content:
              application/json:
              schema: Error
        """
        async with self.AsyncSession() as session:
            accessible = await session.scalar(
                Token.select(session.user_or_token, mode="update").where(
                    Token.id == token_id
                )
            )
            if accessible is None:
                return self.error(
                    "Token not found, or you do not have permission to revoke it."
                )
        try:
            revoke_generation(token_id)
        except TokenLifecycleError as e:
            return self.error(str(e))
        self.push(
            action="baselayer/SHOW_NOTIFICATION",
            payload={"note": "Token generation revoked.", "type": "info"},
        )
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success()


class TokenSeriesRevokeHandler(BaseHandler):
    @auth_or_token
    async def post(self, series_id: str):
        """
        ---
        description: Revoke every generation of a token series
        tags:
          - tokens
        responses:
          200:
            content:
              application/json:
              schema: Success
          400:
            content:
              application/json:
              schema: Error
        """

        async with self.AsyncSession() as session:
            series = await session.scalar(
                TokenSeries.select(session.user_or_token, mode="update").where(
                    TokenSeries.id == series_id
                )
            )
            if series is None:
                return self.error("Token series not found or not accessible.")
        try:
            result = revoke_series(series_id)
        except TokenLifecycleError as e:
            return self.error(str(e))
        self.push(
            action="baselayer/SHOW_NOTIFICATION",
            payload={"note": "Token series revoked.", "type": "info"},
        )
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success(data=result)


class TokenSeriesRotateHandler(BaseHandler):
    @auth_or_token
    async def post(self, series_id: str, *, body: TokenRotateBody = None):
        """
        ---
        description: Rotate a token series, issuing its next generation
        tags:
          - tokens
        responses:
          200:
            content:
              application/json:
              schema: Success
          400:
            content:
              application/json:
              schema: Error
        """
        body = self.parse_body(TokenRotateBody)

        async with self.AsyncSession() as session:
            series = await session.scalar(
                TokenSeries.select(session.user_or_token).where(
                    TokenSeries.id == series_id
                )
            )
            if series is None:
                return self.error("Token series not found or not accessible.")
        try:
            result = rotate_series(
                series_id,
                overlap_seconds=body.overlap_seconds,
                expires_at=body.expires_at,
            )
        except (TokenScopeError, TokenLifecycleError) as e:
            return self.error(str(e))
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success(data=result)


class TokenGenerationHandler(BaseHandler):
    @auth_or_token
    async def post(self, series_id: str, generation: str):
        """Revoke one generation of a series."""
        async with self.AsyncSession() as session:
            accessible = await session.scalar(
                TokenSeries.select(session.user_or_token).where(
                    TokenSeries.id == series_id
                )
            )
            if accessible is None:
                return self.error("Token series not found or not accessible.")
        try:
            revoke_generation_by_ref(series_id, generation)
        except (TokenLifecycleError, ValueError) as e:
            return self.error(str(e))
        self.push(
            action="baselayer/SHOW_NOTIFICATION",
            payload={"note": "Token generation revoked.", "type": "info"},
        )
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success()

    @auth_or_token
    async def delete(self, series_id: str, generation: str):
        """Delete one generation row of a series."""
        async with self.AsyncSession() as session:
            accessible = await session.scalar(
                TokenSeries.select(session.user_or_token, mode="delete").where(
                    TokenSeries.id == series_id
                )
            )
            if accessible is None:
                return self.error("Token series not found or not accessible.")
        try:
            delete_generation_by_ref(series_id, generation)
        except TokenLifecycleError as e:
            return self.error(str(e))
        self.push(action="skyportal/FETCH_USER_PROFILE")
        return self.success()


class LegacyTokenByFingerprintHandler(BaseHandler):
    async def _find_legacy(self, session, fingerprint):
        stmt = (
            Token.select(session.user_or_token, mode="update")
            .where(Token.series_id.is_(None), Token.id.like(f"{fingerprint}%"))
            .order_by(Token.created_at)
        )
        matches = (await session.scalars(stmt)).all()
        if len(matches) != 1:
            return None
        return matches[0]

    @auth_or_token
    async def put(self, fingerprint: str, *, body: TokenPutBody = None):
        body = self.parse_body(TokenPutBody)
        async with self.AsyncSession() as session:
            token = await self._find_legacy(session, fingerprint)
            if token is None:
                return self.error("Legacy token not found.")
            user = self.associated_user_object
            if body.name is not None:
                if body.name.startswith(SERVICE_TOKEN_PREFIX):
                    return self.error(
                        f'A token name cannot start with "{SERVICE_TOKEN_PREFIX}"'
                    )
                token.name = body.name
            if body.acls is not None:
                token_acls = set(body.acls)
                if not all(acl_id in user.permissions for acl_id in token_acls):
                    return self.error(
                        "User has attempted to grant token ACLs they do not have "
                        "access to. Please try again."
                    )
                valid = set(
                    (
                        await session.scalars(
                            ACL.select(session.user_or_token, columns=[ACL.id]).where(
                                ACL.id.in_(token_acls)
                            )
                        )
                    ).all()
                )
                if set(token_acls) != valid or not token_acls:
                    return self.error("Invalid ACL list for token.")
                token.acls = (
                    (
                        await session.scalars(
                            ACL.select(session.user_or_token).where(
                                ACL.id.in_(token_acls)
                            )
                        )
                    )
                    .unique()
                    .all()
                )
            await session.commit()
            self.push(action="skyportal/FETCH_USER_PROFILE")
            return self.success()

    @auth_or_token
    async def delete(self, fingerprint: str):
        async with self.AsyncSession() as session:
            token = await self._find_legacy(session, fingerprint)
            if token is None:
                return self.error("Legacy token not found.")
            await session.delete(token)
            await session.commit()
            self.push(action="skyportal/FETCH_USER_PROFILE")
            return self.success()
