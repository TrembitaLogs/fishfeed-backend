"""Family Mode API endpoints for shared aquarium access."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.aquarium import AquariumResponse
from app.schemas.family import (
    AcceptInviteRequest,
    FamilyListResponse,
    InviteListResponse,
    InviteResponse,
)
from app.services.aquarium import (
    AquariumAccessDeniedError,
    AquariumNotFoundError,
    AquariumOwnerRequiredError,
)
from app.services.family import (
    AlreadyMemberError,
    CannotRemoveOwnerError,
    InviteExpiredError,
    InviteNotFoundError,
    MemberLimitExceededError,
    MemberNotFoundError,
    accept_invite,
    cancel_invite,
    create_invite,
    get_family_members,
    get_invites,
    remove_member,
)

router = APIRouter(tags=["Family"])


@router.get(
    "/aquariums/{aquarium_id}/family",
    response_model=FamilyListResponse,
    summary="List family members",
    responses={
        200: {"description": "List of family members"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def list_family_members(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> FamilyListResponse:
    """Get all family members for an aquarium.

    Available for owner and members.
    Returns owner first, then members sorted by join date.
    """
    try:
        members = await get_family_members(db, aquarium_id, current_user.id)
        return FamilyListResponse(aquarium_id=aquarium_id, members=members)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/aquariums/{aquarium_id}/family/invite",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create invite",
    responses={
        201: {"description": "Invite created"},
        401: {"description": "Not authenticated"},
        403: {"description": "Owner required or member limit exceeded"},
        404: {"description": "Aquarium not found"},
    },
)
async def create_family_invite(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> InviteResponse:
    """Create an invite link for the aquarium.

    Only owner can create invites.
    Checks member limit before creating (Free: 2, Premium: 5).
    Returns 403 if member limit is exceeded.
    """
    try:
        invite = await create_invite(db, aquarium_id, current_user.id)
        return invite
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumOwnerRequiredError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except MemberLimitExceededError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Member limit exceeded ({e.current}/{e.limit}). Upgrade to Premium for more members.",
        ) from None


@router.get(
    "/aquariums/{aquarium_id}/family/invites",
    response_model=InviteListResponse,
    summary="List active invites",
    responses={
        200: {"description": "List of active invites"},
        401: {"description": "Not authenticated"},
        403: {"description": "Owner required"},
        404: {"description": "Aquarium not found"},
    },
)
async def list_family_invites(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> InviteListResponse:
    """Get all active (unused, not expired) invites for an aquarium.

    Only owner can list invites.
    """
    try:
        invites = await get_invites(db, aquarium_id, current_user.id)
        return InviteListResponse(aquarium_id=aquarium_id, invites=invites)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumOwnerRequiredError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.delete(
    "/aquariums/{aquarium_id}/family/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel invite",
    responses={
        204: {"description": "Invite cancelled"},
        401: {"description": "Not authenticated"},
        403: {"description": "Owner required"},
        404: {"description": "Aquarium or invite not found"},
    },
)
async def cancel_family_invite(
    aquarium_id: UUID,
    invite_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Cancel an active invite.

    Only owner can cancel invites.
    """
    try:
        await cancel_invite(db, aquarium_id, invite_id, current_user.id)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumOwnerRequiredError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except InviteNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/family/accept",
    response_model=AquariumResponse,
    summary="Accept invite",
    responses={
        200: {"description": "Invite accepted, returns joined aquarium"},
        400: {"description": "Invalid or expired invite, or already a member"},
        401: {"description": "Not authenticated"},
        403: {"description": "Member limit exceeded"},
        404: {"description": "Invite not found"},
    },
)
async def accept_family_invite(
    data: AcceptInviteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> AquariumResponse:
    """Accept an invite and join an aquarium as a member.

    Validates invite code and expiration.
    Checks member limit before joining (Free: 2, Premium: 5).
    Returns 403 if member limit is exceeded.
    """
    try:
        aquarium = await accept_invite(db, data.invite_code, current_user.id)
        return AquariumResponse.model_validate(aquarium)
    except InviteNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except InviteExpiredError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AlreadyMemberError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except MemberLimitExceededError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Member limit exceeded ({e.current}/{e.limit}). Ask the owner to upgrade to Premium.",
        ) from None


@router.delete(
    "/aquariums/{aquarium_id}/family/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove family member",
    responses={
        204: {"description": "Member removed"},
        400: {"description": "Cannot remove owner"},
        401: {"description": "Not authenticated"},
        403: {"description": "Owner required"},
        404: {"description": "Aquarium or member not found"},
    },
)
async def remove_family_member(
    aquarium_id: UUID,
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Remove a member from the aquarium.

    Only owner can remove members.
    Owner cannot remove themselves.
    """
    try:
        await remove_member(db, aquarium_id, user_id, current_user.id)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumOwnerRequiredError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except CannotRemoveOwnerError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except MemberNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
