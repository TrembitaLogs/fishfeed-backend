"""Family service with business logic for family mode (shared aquarium access)."""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.aquarium import Aquarium, AquariumMember, FamilyInvite
from app.models.user import User
from app.schemas.family import FamilyMemberResponse, InviteResponse
from app.services.aquarium import (
    AquariumNotFoundError,
    AquariumOwnerRequiredError,
    check_access,
)
from app.services.gamification import check_achievements

logger = structlog.get_logger(__name__)

# Premium limits for family members
FREE_MEMBER_LIMIT = 2
PREMIUM_MEMBER_LIMIT = 5

# Invite TTL in days
INVITE_TTL_DAYS = 7


class FamilyError(Exception):
    """Base exception for family errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MemberLimitExceededError(FamilyError):
    """Raised when member limit is exceeded."""

    def __init__(self, current: int, limit: int):
        super().__init__(
            f"Member limit exceeded: {current}/{limit} members",
            status_code=403,
        )
        self.current = current
        self.limit = limit


class InviteNotFoundError(FamilyError):
    """Raised when invite code is not found."""

    def __init__(self, invite_code: str):
        super().__init__(
            f"Invite code '{invite_code}' not found",
            status_code=404,
        )
        self.invite_code = invite_code


class InviteExpiredError(FamilyError):
    """Raised when invite code has expired."""

    def __init__(self, invite_code: str):
        super().__init__(
            f"Invite code '{invite_code}' has expired",
            status_code=400,
        )
        self.invite_code = invite_code


class AlreadyMemberError(FamilyError):
    """Raised when user is already a member of the aquarium."""

    def __init__(self, aquarium_id: UUID, user_id: UUID):
        super().__init__(
            f"User '{user_id}' is already a member of aquarium '{aquarium_id}'",
            status_code=400,
        )
        self.aquarium_id = aquarium_id
        self.user_id = user_id


class MemberNotFoundError(FamilyError):
    """Raised when member is not found in the aquarium."""

    def __init__(self, aquarium_id: UUID, user_id: UUID):
        super().__init__(
            f"Member '{user_id}' not found in aquarium '{aquarium_id}'",
            status_code=404,
        )
        self.aquarium_id = aquarium_id
        self.user_id = user_id


class CannotRemoveOwnerError(FamilyError):
    """Raised when attempting to remove the owner from the aquarium."""

    def __init__(self, aquarium_id: UUID):
        super().__init__(
            f"Cannot remove owner from aquarium '{aquarium_id}'",
            status_code=400,
        )
        self.aquarium_id = aquarium_id


def _generate_invite_code() -> str:
    """Generate a unique 8-character invite code.

    Returns:
        8-character alphanumeric string.
    """
    return secrets.token_urlsafe(6)[:8]


def _build_invite_link(invite_code: str) -> str:
    """Build invite link from invite code.

    Args:
        invite_code: The invite code.

    Returns:
        Full invite link URL.
    """
    settings = get_settings()
    return f"{settings.INVITE_BASE_URL}/{invite_code}"


async def _get_member_count(db: AsyncSession, aquarium_id: UUID) -> int:
    """Get current member count for aquarium.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.

    Returns:
        Number of members.
    """
    stmt = (
        select(func.count())
        .select_from(AquariumMember)
        .where(AquariumMember.aquarium_id == aquarium_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def _get_member_limit(db: AsyncSession, owner_id: UUID) -> int:
    """Get member limit based on owner's subscription status.

    Args:
        db: Database session.
        owner_id: Owner user ID.

    Returns:
        Maximum allowed members.
    """
    stmt = select(User.subscription_status).where(User.id == owner_id)
    result = await db.execute(stmt)
    subscription_status = result.scalar_one_or_none()

    if subscription_status == "premium":
        return PREMIUM_MEMBER_LIMIT
    return FREE_MEMBER_LIMIT


async def _check_member_limit(
    db: AsyncSession,
    aquarium_id: UUID,
    owner_id: UUID,
) -> tuple[int, int]:
    """Check if member limit allows adding new members.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        owner_id: Owner user ID.

    Returns:
        Tuple of (current_count, limit).

    Raises:
        MemberLimitExceededError: If limit is exceeded.
    """
    current_count = await _get_member_count(db, aquarium_id)
    limit = await _get_member_limit(db, owner_id)

    if current_count >= limit:
        raise MemberLimitExceededError(current_count, limit)

    return current_count, limit


async def get_family_members(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> list[FamilyMemberResponse]:
    """Get all family members for an aquarium.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        List of family members, owner first, then sorted by joined_at.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    # Check access (will raise if no access)
    await check_access(db, aquarium_id, user_id)

    # Query members with user info
    stmt = (
        select(
            AquariumMember.user_id,
            AquariumMember.role,
            AquariumMember.joined_at,
            User.nickname,
            User.avatar_key,
        )
        .join(User, AquariumMember.user_id == User.id)
        .where(AquariumMember.aquarium_id == aquarium_id)
        .order_by(
            # Owner first (role='owner' sorts before 'member' alphabetically reversed)
            AquariumMember.role.desc(),
            AquariumMember.joined_at.asc(),
        )
    )

    result = await db.execute(stmt)
    rows = result.all()

    members = [
        FamilyMemberResponse(
            user_id=row.user_id,
            nickname=row.nickname,
            avatar_key=row.avatar_key,
            role=row.role,
            joined_at=row.joined_at,
        )
        for row in rows
    ]

    logger.debug(f"Retrieved {len(members)} family members for aquarium '{aquarium_id}'")
    return members


async def create_invite(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> InviteResponse:
    """Create a new invite for an aquarium.

    Only the owner can create invites.
    Checks member limit before creating invite.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID (must be owner).

    Returns:
        InviteResponse with invite code and link.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumOwnerRequiredError: If user is not owner.
        MemberLimitExceededError: If member limit is exceeded.
    """
    # Check access and verify owner
    aquarium, role = await check_access(db, aquarium_id, user_id)

    if role != "owner":
        raise AquariumOwnerRequiredError(aquarium_id)

    # Check member limit before creating invite
    current_count, limit = await _check_member_limit(db, aquarium_id, aquarium.owner_id)

    # Generate unique invite code
    invite_code = _generate_invite_code()

    # Create invite record
    expires_at = datetime.now(UTC) + timedelta(days=INVITE_TTL_DAYS)

    invite = FamilyInvite(
        aquarium_id=aquarium_id,
        invite_code=invite_code,
        created_by=user_id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.flush()

    invite_link = _build_invite_link(invite_code)

    logger.info(
        f"Created invite '{invite_code}' for aquarium '{aquarium_id}' "
        f"by user '{user_id}' (members: {current_count}/{limit})"
    )

    return InviteResponse(
        invite_code=invite_code,
        invite_link=invite_link,
        expires_at=expires_at,
    )


async def _is_member(db: AsyncSession, aquarium_id: UUID, user_id: UUID) -> bool:
    """Check if user is already a member of the aquarium.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID.

    Returns:
        True if user is a member, False otherwise.
    """
    stmt = select(AquariumMember).where(
        AquariumMember.aquarium_id == aquarium_id,
        AquariumMember.user_id == user_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def accept_invite(
    db: AsyncSession,
    invite_code: str,
    user_id: UUID,
) -> Aquarium:
    """Accept an invite and join an aquarium as a member.

    Args:
        db: Database session.
        invite_code: The invite code to accept.
        user_id: User ID accepting the invite.

    Returns:
        Aquarium object the user joined.

    Raises:
        InviteNotFoundError: If invite code not found.
        InviteExpiredError: If invite has expired.
        AlreadyMemberError: If user is already a member.
        MemberLimitExceededError: If member limit is exceeded.
    """
    # Find invite by code
    stmt = (
        select(FamilyInvite)
        .where(FamilyInvite.invite_code == invite_code)
        .where(FamilyInvite.used_by.is_(None))
    )
    result = await db.execute(stmt)
    invite = result.scalar_one_or_none()

    if invite is None:
        raise InviteNotFoundError(invite_code)

    # Check if expired
    now = datetime.now(UTC)
    if invite.expires_at < now:
        raise InviteExpiredError(invite_code)

    # Check if user is already a member
    if await _is_member(db, invite.aquarium_id, user_id):
        raise AlreadyMemberError(invite.aquarium_id, user_id)

    # Get aquarium and check member limit
    aquarium_stmt = (
        select(Aquarium)
        .where(Aquarium.id == invite.aquarium_id)
        .where(Aquarium.deleted_at.is_(None))
    )
    aquarium_result = await db.execute(aquarium_stmt)
    aquarium = aquarium_result.scalar_one_or_none()

    if aquarium is None:
        raise AquariumNotFoundError(invite.aquarium_id)

    # Check member limit before adding
    await _check_member_limit(db, aquarium.id, aquarium.owner_id)

    # Create member record
    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=user_id,
        role="member",
    )
    db.add(member)

    # Mark invite as used
    invite.used_by = user_id
    invite.used_at = now

    await db.flush()
    await db.refresh(aquarium)

    # TODO: Send push notification to owner when NotificationService is implemented
    # notify_owner_new_member(aquarium.id, user_nickname)
    logger.info(
        f"User '{user_id}' accepted invite '{invite_code}' "
        f"and joined aquarium '{aquarium.id}'"
    )

    # Check achievements for aquarium owner (they're gaining family members)
    try:
        await check_achievements(db, aquarium.owner_id)
    except Exception as e:
        logger.error(f"Failed to check achievements after accepting invite: {e}")

    return aquarium


async def remove_member(
    db: AsyncSession,
    aquarium_id: UUID,
    member_id: UUID,
    requesting_user_id: UUID,
) -> None:
    """Remove a member from an aquarium.

    Only the owner can remove members.
    Owner cannot remove themselves.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        member_id: User ID of the member to remove.
        requesting_user_id: User ID making the request (must be owner).

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If requesting user doesn't have access.
        AquariumOwnerRequiredError: If requesting user is not owner.
        CannotRemoveOwnerError: If trying to remove the owner.
        MemberNotFoundError: If member is not found in the aquarium.
    """
    # Check access and verify owner
    aquarium, role = await check_access(db, aquarium_id, requesting_user_id)

    if role != "owner":
        raise AquariumOwnerRequiredError(aquarium_id)

    # Cannot remove owner
    if member_id == aquarium.owner_id:
        raise CannotRemoveOwnerError(aquarium_id)

    # Find member
    stmt = select(AquariumMember).where(
        AquariumMember.aquarium_id == aquarium_id,
        AquariumMember.user_id == member_id,
    )
    result = await db.execute(stmt)
    member = result.scalar_one_or_none()

    if member is None:
        raise MemberNotFoundError(aquarium_id, member_id)

    # Remove member
    await db.delete(member)
    await db.flush()

    # TODO: Send push notification to removed member when NotificationService is implemented
    # notify_member_removed(member_id, aquarium.name)
    logger.info(
        f"Owner '{requesting_user_id}' removed member '{member_id}' "
        f"from aquarium '{aquarium_id}'"
    )
