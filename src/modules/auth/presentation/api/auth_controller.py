from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.modules.auth.domain.services import AuthService, TokenPair
from src.modules.auth.presentation.dtos.auth import (
    AuthenticatedResponse,
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    SignupRequest,
    TokenPairResponse,
    UserResponse,
)
from src.modules.tenants.domain.models import User
from src.modules.tenants.presentation.dependencies import CredentialsDep, bearer_scheme
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
    },
)
async def signup(
    payload: SignupRequest, service: AuthServiceDep
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
    },
)
async def login(
    payload: LoginRequest, service: AuthServiceDep
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
    },
)
async def refresh(
    payload: RefreshRequest, service: AuthServiceDep
) -> ApiResponse[AuthenticatedResponse]:
    user, tokens = await service.refresh(payload.refresh_token)
    return ApiResponse.ok(_authenticated(user, tokens))


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
