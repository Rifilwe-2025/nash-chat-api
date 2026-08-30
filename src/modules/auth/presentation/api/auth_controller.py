from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.modules.auth.domain.services import AuthService, TokenPair
from src.modules.auth.presentation.dependencies import CredentialThrottleDep
from src.modules.auth.presentation.dtos.auth import (
    AuthenticatedResponse,
    ChangePasswordRequest,
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    SignupRequest,
    TokenPairResponse,
    UserResponse,
)
from src.modules.tenants.domain.models import User
from src.modules.tenants.presentation.dependencies import (
    AuthenticatedDep,
    CredentialsDep,
    bearer_scheme,
)
from src.shared.database.dependencies import SessionDep
from src.shared.exceptions import UnauthorizedException
from src.shared.responses import ApiResponse, create_router

router = create_router(prefix="/auth", tags=["auth"])


def get_auth_service(session: SessionDep) -> AuthService:
    return AuthService(session)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]

UNAUTHORIZED_RESPONSE = {
    "description": (
        "Credentials are missing, invalid, or the token has been revoked "
        "(`UNAUTHORIZED`, `INVALID_CREDENTIALS`, `INVALID_TOKEN`, `TOKEN_REVOKED`)."
    )
}


def _authenticated(user: User, tokens: TokenPair) -> AuthenticatedResponse:
    return AuthenticatedResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.value,
            tenant_id=user.tenant_id,
            is_platform_admin=user.is_platform_admin,
            must_change_password=user.must_change_password,
        ),
        tokens=TokenPairResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_at=tokens.expires_at,
        ),
    )


@router.post(
    "/signup",
    response_model=ApiResponse[AuthenticatedResponse],
    status_code=201,
    summary="Create an account",
    description=(
        "Registers a new tenant together with its first user, who becomes the owner, and returns "
        "a token pair so the caller is signed in immediately. Email addresses are unique across "
        "the platform."
    ),
    responses={
        201: {"description": "The tenant and its owner were created."},
        409: {"description": "That email is already registered (`EMAIL_TAKEN`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
        429: {
            "description": (
                "Too many attempts from this address in the last minute (`RATE_LIMITED`). The "
                "`X-RateLimit-*` headers on every response say how much allowance is left, and "
                "`Retry-After` says when to come back."
            )
        },
    },
)
async def signup(
    payload: SignupRequest, service: AuthServiceDep, _: CredentialThrottleDep
) -> ApiResponse[AuthenticatedResponse]:
    user, tokens = await service.signup(
        email=payload.email,
        password=payload.password,
        tenant_name=payload.tenant_name,
        full_name=payload.full_name,
    )
    return ApiResponse.ok(_authenticated(user, tokens), message="Account created.")


@router.post(
    "/login",
    response_model=ApiResponse[AuthenticatedResponse],
    summary="Sign in",
    description=(
        "Exchanges email and password for a token pair. **Every token issued earlier is revoked** "
        "— signing in elsewhere ends existing sessions."
    ),
    responses={
        200: {"description": "Signed in."},
        401: {"description": "Email or password is incorrect (`INVALID_CREDENTIALS`)."},
        429: {
            "description": (
                "Too many attempts from this address in the last minute (`RATE_LIMITED`). The "
                "`X-RateLimit-*` headers on every response say how much allowance is left, and "
                "`Retry-After` says when to come back."
            )
        },
    },
)
async def login(
    payload: LoginRequest, service: AuthServiceDep, _: CredentialThrottleDep
) -> ApiResponse[AuthenticatedResponse]:
    user, tokens = await service.login(email=payload.email, password=payload.password)
    return ApiResponse.ok(_authenticated(user, tokens))


@router.post(
    "/refresh",
    response_model=ApiResponse[AuthenticatedResponse],
    summary="Rotate the token pair",
    description=(
        "Exchanges a valid refresh token for a new pair. The presented token — and every other "
        "token held by that user — is revoked in the same step, so a refresh token works exactly "
        "once. Reusing one returns 401."
    ),
    responses={
        200: {"description": "A new pair was issued."},
        401: {
            "description": (
                "The refresh token is invalid, already used, revoked, or expired "
                "(`INVALID_TOKEN`, `TOKEN_REVOKED`)."
            )
        },
        429: {
            "description": (
                "Too many attempts from this address in the last minute (`RATE_LIMITED`). The "
                "`X-RateLimit-*` headers on every response say how much allowance is left, and "
                "`Retry-After` says when to come back."
            )
        },
    },
)
async def refresh(
    payload: RefreshRequest, service: AuthServiceDep, _: CredentialThrottleDep
) -> ApiResponse[AuthenticatedResponse]:
    user, tokens = await service.refresh(payload.refresh_token)
    return ApiResponse.ok(_authenticated(user, tokens))


@router.post(
    "/password",
    response_model=ApiResponse[AuthenticatedResponse],
    summary="Change your password",
    description=(
        "Replaces your password and returns a fresh token pair.\n\n"
        "**Every other session ends.** Issuing a pair revokes every token held before it, which is "
        "most of the point: a password is changed because it may be known, and the sessions that "
        "may be using it should stop.\n\n"
        "The current password is required even though you are signed in. An access token is a "
        "bearer credential — a borrowed laptop, a copied header — and knowing the password is the "
        "only evidence that the person changing it owns the account.\n\n"
        "**This is the one endpoint an account with `mustChangePassword` can call.** The "
        "administrator a deployment creates from its environment starts in that state, because a "
        "password that lives in a configuration file is known to everyone who can read it. Calling "
        "this clears the flag and the rest of the API opens up."
    ),
    responses={
        200: {"description": "The password was changed; a new token pair is returned."},
        401: {
            "description": (
                "The access token is invalid, or `currentPassword` is wrong "
                "(`UNAUTHORIZED`, `INVALID_TOKEN`, `INVALID_CREDENTIALS`)."
            )
        },
        409: {"description": "The new password matches the current one (`PASSWORD_UNCHANGED`)."},
        422: {"description": "The new password is too short (`VALIDATION_ERROR`)."},
    },
)
async def change_password(
    payload: ChangePasswordRequest, service: AuthServiceDep, authenticated: AuthenticatedDep
) -> ApiResponse[AuthenticatedResponse]:
    user, tokens = await service.change_password(
        authenticated.user, payload.current_password, payload.new_password
    )
    return ApiResponse.ok(_authenticated(user, tokens), message="Password changed.")


@router.post(
    "/logout",
    response_model=ApiResponse[LogoutResponse],
    summary="Sign out",
    description=(
        "Revokes every token belonging to the caller, not only the one presented. Takes effect "
        "immediately — revoked access tokens are rejected on their next use rather than lasting "
        "until they expire."
    ),
    responses={200: {"description": "Tokens revoked."}, 401: UNAUTHORIZED_RESPONSE},
    dependencies=[Depends(bearer_scheme)],
)
async def logout(
    service: AuthServiceDep, credentials: CredentialsDep
) -> ApiResponse[LogoutResponse]:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedException(
            "Provide an access token as 'Authorization: Bearer <token>'.", code="UNAUTHORIZED"
        )
    revoked = await service.logout(credentials.credentials)
    return ApiResponse.ok(LogoutResponse(revoked=revoked), message="Signed out.")


__all__ = ["router"]
