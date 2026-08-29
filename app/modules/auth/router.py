"""Auth HTTP routes (design/03 § auth)."""

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_db
from app.core.errors import err
from app.core.ratelimit import rate_limit
from app.core.security import new_token
from app.core.time import business_today
from app.modules.auth import repo, service
from app.modules.auth.deps import SUPERUSER_ROLE, get_current_session, get_current_user
from app.modules.auth.models import Applicant, Representation, Role, Session, User
from app.modules.auth.permissions import PERMISSIONS
from app.modules.auth.schemas import (
    AddRepresentationIn,
    ApplicantOut,
    AttachLegalIn,
    AttachLegalOut,
    CompleteRegistrationIn,
    ContactUpdateIn,
    EimzoChallengeOut,
    EimzoLoginIn,
    LoginIn,
    LoginOut,
    MeOut,
    MfaIn,
    OneIdAuthorizeOut,
    OtpRequestIn,
    OtpVerifyIn,
    OtpVerifyOut,
    PasswordChangeIn,
    RepresentationOut,
    RoleOut,
    UserOut,
    ZoneOut,
)
from app.modules.integrations.adapters.oneid import get_oneid_adapter

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    settings = get_settings()
    secure = settings.resolve_cookie_secure()
    samesite = settings.resolve_cookie_samesite()
    response.set_cookie("session", token, httponly=True, samesite=samesite, secure=secure, path="/")
    response.set_cookie(
        "csrf_token", csrf, httponly=False, samesite=samesite, secure=secure, path="/"
    )


async def _me_out(db: AsyncSession, user: User, role: Role, csrf_token: str) -> MeOut:
    """Shared by every session-returning/profile-returning route: GET /auth/me,
    /auth/mfa/verify, /auth/oneid/callback, /auth/eimzo/login,
    /auth/complete-registration, and PATCH /auth/me — all return the same shape.

    sys_admin passes require_permission without consulting codes (ruling 2), so its
    `permissions` here is the whole registry rather than its (usually empty) personal
    grants — otherwise the adminka would render "no rights" for the one user who
    holds every one of them.
    """
    is_superuser = role.code == SUPERUSER_ROLE
    codes = sorted(PERMISSIONS) if is_superuser else sorted(await repo.permission_codes(db, user))
    applicant_out: ApplicantOut | None = None
    representations: list[RepresentationOut] = []
    registration_complete = True
    if role.code == "applicant":
        own = await repo.get_own_applicant(db, user.id)
        applicant_out = ApplicantOut.model_validate(own, from_attributes=True) if own else None
        registration_complete = own is not None
        representations = [
            RepresentationOut(
                id=rep.id,
                applicant=ApplicantOut.model_validate(legal, from_attributes=True),
                basis=rep.basis,
                valid_from=rep.valid_from,
                valid_until=rep.valid_until,
                status=rep.status,
            )
            for rep, legal in await repo.effective_representations(db, user.id, business_today())
        ]
    return MeOut(
        user=UserOut.model_validate(user, from_attributes=True),
        role=RoleOut.model_validate(role, from_attributes=True),
        permissions=codes,
        zone=ZoneOut(
            region_id=user.region_id,
            district_id=user.district_id,
            organization_id=user.organization_id,
        ),
        csrf_token=csrf_token,
        is_superuser=is_superuser,
        applicant=applicant_out,
        representations=representations,
        registration_complete=registration_complete,
    )


@router.get("/me", response_model=MeOut)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    # session_row depends on get_current_session, which get_current_user already
    # depends on — FastAPI caches it per-request, so this doesn't re-run the chain.
    # Carrying csrf_token here (ruling 3) lets a page reload recover it without a
    # fresh login.
    role = await repo.get_role(db, user.role_id)
    assert role is not None  # FK guarantees it
    return await _me_out(db, user, role, session_row.csrf_token)


@router.post("/logout", status_code=204)
async def logout(
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> None:
    await service.revoke_session(db, session_row, reason="logout")
    response.delete_cookie("session")
    response.delete_cookie("csrf_token")


@router.post(
    "/login",
    response_model=LoginOut,
    dependencies=[Depends(rate_limit("login", "ratelimit_login_per_minute"))],
)
async def login(
    body: LoginIn, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> LoginOut:
    token = await service.login_password(
        db,
        login=body.login,
        password=body.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return LoginOut(mfa_required=True, mfa_token=token)


@router.post("/mfa/verify", response_model=MeOut)
async def mfa_verify(
    body: MfaIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    user, _row, token, csrf = await service.verify_mfa(
        db,
        mfa_token=body.mfa_token,
        code=body.code,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    _set_session_cookies(response, token, csrf)
    role = await repo.get_role(db, user.role_id)
    assert role is not None
    return await _me_out(db, user, role, csrf)


@router.get("/oneid/authorize", response_model=OneIdAuthorizeOut)
async def oneid_authorize(response: Response) -> OneIdAuthorizeOut:
    settings = get_settings()
    state = new_token()
    url = get_oneid_adapter().authorize_url(
        state=state, redirect_uri=settings.oneid_redirect_uri, scope=settings.oneid_scope
    )
    response.set_cookie(
        "oneid_state",
        state,
        httponly=True,
        max_age=600,
        samesite=settings.resolve_cookie_samesite(),
        secure=settings.resolve_cookie_secure(),
        path="/",
    )
    return OneIdAuthorizeOut(redirect_url=url)


@router.get("/oneid/callback", response_model=MeOut)
async def oneid_callback(
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    expected = request.cookies.get("oneid_state")
    if not expected or not secrets.compare_digest(state, expected):
        raise err("ERR-AUTH-006")
    user, _row, token, csrf = await service.login_via_oneid(
        db,
        code=code,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    response.delete_cookie("oneid_state")
    _set_session_cookies(response, token, csrf)
    role = await repo.get_role(db, user.role_id)
    assert role is not None
    return await _me_out(db, user, role, csrf)


@router.post(
    "/eimzo/challenge",
    response_model=EimzoChallengeOut,
    dependencies=[Depends(rate_limit("eimzo_challenge", "ratelimit_challenge_per_minute"))],
)
async def eimzo_challenge(db: Annotated[AsyncSession, Depends(get_db)]) -> EimzoChallengeOut:
    return EimzoChallengeOut(challenge=await service.issue_eimzo_challenge(db))


@router.post("/eimzo/login", response_model=MeOut)
async def eimzo_login(
    body: EimzoLoginIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    user, _row, token, csrf = await service.login_via_eimzo(
        db,
        signed_challenge=body.signed_challenge,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    _set_session_cookies(response, token, csrf)
    role = await repo.get_role(db, user.role_id)
    assert role is not None
    return await _me_out(db, user, role, csrf)


@router.post("/password/change", status_code=204)
async def password_change(
    body: PasswordChangeIn,
    user: Annotated[User, Depends(get_current_user)],
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await service.change_password(
        db, user, old=body.old_password, new=body.new_password, current_session_id=session_row.id
    )


@router.post(
    "/otp/request",
    status_code=204,
    dependencies=[Depends(rate_limit("otp_request", "ratelimit_otp_per_minute"))],
)
async def otp_request(
    body: OtpRequestIn, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    await service.request_otp(
        db,
        target_type=body.target_type,
        target=body.target,
        purpose=body.purpose,
        ip=request.client.host if request.client else None,
    )


@router.post("/otp/verify", response_model=OtpVerifyOut)
async def otp_verify(
    body: OtpVerifyIn, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> OtpVerifyOut:
    token = await service.verify_otp(
        db,
        target=body.target,
        code=body.code,
        purpose=body.purpose,
        ip=request.client.host if request.client else None,
    )
    return OtpVerifyOut(otp_token=token)


@router.post("/complete-registration", response_model=MeOut)
async def complete_registration(
    body: CompleteRegistrationIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    await service.complete_registration(
        db,
        user,
        privacy_policy_version=body.consents.privacy_policy,
        offer_version=body.consents.offer,
        phone=body.phone,
        otp_token=body.otp_token,
        email=str(body.email) if body.email else None,
        region_id=body.region_id,
        district_id=body.district_id,
        address=body.address,
        ip=request.client.host if request.client else None,
    )
    role = await repo.get_role(db, user.role_id)
    assert role is not None
    return await _me_out(db, user, role, session_row.csrf_token)


def _representation_out(rep: Representation, applicant: Applicant) -> RepresentationOut:
    return RepresentationOut(
        id=rep.id,
        applicant=ApplicantOut.model_validate(applicant, from_attributes=True),
        basis=rep.basis,
        valid_from=rep.valid_from,
        valid_until=rep.valid_until,
        status=rep.status,
    )


@router.post("/applicants", status_code=201, response_model=AttachLegalOut)
async def attach_legal(
    body: AttachLegalIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AttachLegalOut:
    applicant, representation = await service.attach_legal(
        db,
        user,
        stir=body.stir,
        basis=body.basis,
        signed_challenge=body.signed_challenge,
        poa_file_id=body.poa_file_id,
        valid_until=body.valid_until,
        name=body.name,
        ip=request.client.host if request.client else None,
    )
    return AttachLegalOut(
        applicant=ApplicantOut.model_validate(applicant, from_attributes=True),
        representation=_representation_out(representation, applicant),
    )


@router.post(
    "/applicants/{applicant_id}/representations", status_code=201, response_model=RepresentationOut
)
async def add_representation(
    applicant_id: uuid.UUID,
    body: AddRepresentationIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RepresentationOut:
    representation, applicant = await service.add_representation(
        db,
        user,
        applicant_id=applicant_id,
        user_pinfl=body.user_pinfl,
        basis=body.basis,
        signed_challenge=body.signed_challenge,
        poa_file_id=body.poa_file_id,
        valid_until=body.valid_until,
        ip=request.client.host if request.client else None,
    )
    return _representation_out(representation, applicant)


@router.patch("/me", response_model=MeOut)
async def patch_me(
    body: ContactUpdateIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    await service.update_contact(
        db,
        user,
        phone=body.phone,
        email=str(body.email) if body.email else None,
        otp_token=body.otp_token,
        ip=request.client.host if request.client else None,
    )
    role = await repo.get_role(db, user.role_id)
    assert role is not None
    return await _me_out(db, user, role, session_row.csrf_token)
