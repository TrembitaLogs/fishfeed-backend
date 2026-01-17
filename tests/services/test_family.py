"""Integration tests for family service."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import AquariumMember, FamilyInvite
from app.models.user import User
from app.schemas.aquarium import AquariumCreate
from app.services.aquarium import (
    AquariumAccessDeniedError,
    AquariumOwnerRequiredError,
    create_aquarium,
)
from app.services.family import (
    FREE_MEMBER_LIMIT,
    INVITE_TTL_DAYS,
    PREMIUM_MEMBER_LIMIT,
    AlreadyMemberError,
    CannotRemoveOwnerError,
    InviteExpiredError,
    InviteNotFoundError,
    MemberLimitExceededError,
    MemberNotFoundError,
    _generate_invite_code,
    accept_invite,
    create_invite,
    get_family_members,
    remove_member,
)


async def cleanup_family_data(session: AsyncSession) -> None:
    """Helper to cleanup family-related data."""
    await session.execute(text("DELETE FROM family_invites"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
    subscription_status: str = "free",
    nickname: str | None = None,
    avatar_url: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
        subscription_status=subscription_status,
        nickname=nickname,
        avatar_url=avatar_url,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


# _generate_invite_code tests


def test_generate_invite_code_length():
    """Test that generated invite code is 8 characters."""
    code = _generate_invite_code()
    assert len(code) == 8


def test_generate_invite_code_unique():
    """Test that generated invite codes are unique."""
    codes = {_generate_invite_code() for _ in range(100)}
    assert len(codes) == 100


def test_generate_invite_code_alphanumeric():
    """Test that generated invite codes contain only URL-safe characters."""
    for _ in range(50):
        code = _generate_invite_code()
        # token_urlsafe can contain alphanumeric, - and _
        assert all(c.isalnum() or c in "-_" for c in code)


# get_family_members tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_family_members_returns_owner(async_session: AsyncSession):
    """Test that get_family_members returns the owner."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(
            async_session,
            email="owner@example.com",
            nickname="OwnerNick",
            avatar_url="https://example.com/avatar.png",
        )
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        members = await get_family_members(async_session, aquarium.id, owner.id)

        assert len(members) == 1
        assert members[0].user_id == owner.id
        assert members[0].role == "owner"
        assert members[0].nickname == "OwnerNick"
        assert members[0].avatar_url == "https://example.com/avatar.png"
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_family_members_returns_all_members(async_session: AsyncSession):
    """Test that get_family_members returns owner and all members."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member1 = await create_test_user(
            async_session, email="member1@example.com", nickname="Member1"
        )
        member2 = await create_test_user(
            async_session, email="member2@example.com", nickname="Member2"
        )

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add members
        am1 = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member1.id,
            role="member",
        )
        am2 = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member2.id,
            role="member",
        )
        async_session.add_all([am1, am2])
        await async_session.commit()

        members = await get_family_members(async_session, aquarium.id, owner.id)

        assert len(members) == 3
        roles = [m.role for m in members]
        assert roles.count("owner") == 1
        assert roles.count("member") == 2
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_family_members_owner_first(async_session: AsyncSession):
    """Test that get_family_members returns owner as first in list."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        members = await get_family_members(async_session, aquarium.id, owner.id)

        assert members[0].role == "owner"
        assert members[0].user_id == owner.id
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_family_members_member_can_view(async_session: AsyncSession):
    """Test that member can view family members list."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        # Member should be able to view
        members = await get_family_members(async_session, aquarium.id, member.id)

        assert len(members) == 2
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_family_members_raises_access_denied(async_session: AsyncSession):
    """Test that get_family_members raises error for non-members."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        stranger = await create_test_user(async_session, email="stranger@example.com")

        data = AquariumCreate(name="Private Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        with pytest.raises(AquariumAccessDeniedError):
            await get_family_members(async_session, aquarium.id, stranger.id)
    finally:
        await cleanup_family_data(async_session)


# create_invite tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_returns_invite_response(async_session: AsyncSession):
    """Test that create_invite returns valid InviteResponse."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        invite = await create_invite(async_session, aquarium.id, owner.id)

        assert invite.invite_code is not None
        assert len(invite.invite_code) == 8
        assert invite.invite_link is not None
        assert invite.invite_code in invite.invite_link
        assert invite.expires_at is not None
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_ttl_7_days(async_session: AsyncSession):
    """Test that invite expires in 7 days."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        before = datetime.now(UTC)
        invite = await create_invite(async_session, aquarium.id, owner.id)
        after = datetime.now(UTC)

        expected_min = before + timedelta(days=INVITE_TTL_DAYS)
        expected_max = after + timedelta(days=INVITE_TTL_DAYS)

        assert expected_min <= invite.expires_at <= expected_max
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_saves_to_database(async_session: AsyncSession):
    """Test that create_invite saves invite to database."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        invite = await create_invite(async_session, aquarium.id, owner.id)

        # Check database
        from sqlalchemy import select

        stmt = select(FamilyInvite).where(FamilyInvite.invite_code == invite.invite_code)
        result = await async_session.execute(stmt)
        db_invite = result.scalar_one_or_none()

        assert db_invite is not None
        assert db_invite.aquarium_id == aquarium.id
        assert db_invite.created_by == owner.id
        assert db_invite.used_by is None
        assert db_invite.used_at is None
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_only_owner_can_create(async_session: AsyncSession):
    """Test that only owner can create invite."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Shared Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        # Member should not be able to create invite
        with pytest.raises(AquariumOwnerRequiredError):
            await create_invite(async_session, aquarium.id, member.id)
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_invite_link_format(async_session: AsyncSession):
    """Test that invite_link has correct format."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        invite = await create_invite(async_session, aquarium.id, owner.id)

        assert invite.invite_link.startswith("fishfeed://invite/")
        assert invite.invite_link.endswith(invite.invite_code)
    finally:
        await cleanup_family_data(async_session)


# Member limit tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_free_user_limit(async_session: AsyncSession):
    """Test that free user cannot create invite when limit reached."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(
            async_session, email="owner@example.com", subscription_status="free"
        )
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add members up to limit (owner counts as 1)
        for i in range(FREE_MEMBER_LIMIT - 1):
            member = await create_test_user(
                async_session, email=f"member{i}@example.com"
            )
            am = AquariumMember(
                aquarium_id=aquarium.id,
                user_id=member.id,
                role="member",
            )
            async_session.add(am)
        await async_session.commit()

        # Now at limit, should fail
        with pytest.raises(MemberLimitExceededError) as exc_info:
            await create_invite(async_session, aquarium.id, owner.id)

        assert exc_info.value.current == FREE_MEMBER_LIMIT
        assert exc_info.value.limit == FREE_MEMBER_LIMIT
        assert exc_info.value.status_code == 403
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_premium_user_higher_limit(async_session: AsyncSession):
    """Test that premium user has higher member limit."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(
            async_session, email="owner@example.com", subscription_status="premium"
        )
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add members up to free limit (owner counts as 1)
        for i in range(FREE_MEMBER_LIMIT - 1):
            member = await create_test_user(
                async_session, email=f"member{i}@example.com"
            )
            am = AquariumMember(
                aquarium_id=aquarium.id,
                user_id=member.id,
                role="member",
            )
            async_session.add(am)
        await async_session.commit()

        # Premium user should still be able to create invite
        invite = await create_invite(async_session, aquarium.id, owner.id)
        assert invite is not None
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_premium_user_limit(async_session: AsyncSession):
    """Test that premium user cannot exceed premium limit."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(
            async_session, email="owner@example.com", subscription_status="premium"
        )
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add members up to premium limit (owner counts as 1)
        for i in range(PREMIUM_MEMBER_LIMIT - 1):
            member = await create_test_user(
                async_session, email=f"member{i}@example.com"
            )
            am = AquariumMember(
                aquarium_id=aquarium.id,
                user_id=member.id,
                role="member",
            )
            async_session.add(am)
        await async_session.commit()

        # Now at premium limit, should fail
        with pytest.raises(MemberLimitExceededError) as exc_info:
            await create_invite(async_session, aquarium.id, owner.id)

        assert exc_info.value.current == PREMIUM_MEMBER_LIMIT
        assert exc_info.value.limit == PREMIUM_MEMBER_LIMIT
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_invite_unique_codes(async_session: AsyncSession):
    """Test that multiple invites have unique codes."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(
            async_session, email="owner@example.com", subscription_status="premium"
        )
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Create multiple invites
        codes = set()
        for _ in range(5):
            invite = await create_invite(async_session, aquarium.id, owner.id)
            codes.add(invite.invite_code)

        # All codes should be unique
        assert len(codes) == 5
    finally:
        await cleanup_family_data(async_session)


# accept_invite tests


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_success(async_session: AsyncSession):
    """Test that accepting invite adds user as member."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        new_member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Create and accept invite
        invite = await create_invite(async_session, aquarium.id, owner.id)
        result = await accept_invite(async_session, invite.invite_code, new_member.id)

        assert result.id == aquarium.id

        # Verify member was added
        members = await get_family_members(async_session, aquarium.id, owner.id)
        assert len(members) == 2
        member_ids = [m.user_id for m in members]
        assert new_member.id in member_ids
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_marks_invite_as_used(async_session: AsyncSession):
    """Test that accepting invite marks it as used."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        new_member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        invite = await create_invite(async_session, aquarium.id, owner.id)
        await accept_invite(async_session, invite.invite_code, new_member.id)

        # Check invite is marked as used
        from sqlalchemy import select

        stmt = select(FamilyInvite).where(FamilyInvite.invite_code == invite.invite_code)
        result = await async_session.execute(stmt)
        db_invite = result.scalar_one()

        assert db_invite.used_by == new_member.id
        assert db_invite.used_at is not None
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_not_found(async_session: AsyncSession):
    """Test that accepting non-existent invite raises error."""
    await cleanup_family_data(async_session)
    try:
        user = await create_test_user(async_session, email="user@example.com")

        with pytest.raises(InviteNotFoundError) as exc_info:
            await accept_invite(async_session, "nonexist", user.id)

        assert exc_info.value.status_code == 404
        assert exc_info.value.invite_code == "nonexist"
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_expired(async_session: AsyncSession):
    """Test that accepting expired invite raises error."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        new_member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Create invite and manually expire it
        invite = await create_invite(async_session, aquarium.id, owner.id)

        # Update expires_at to past
        from sqlalchemy import update

        stmt = (
            update(FamilyInvite)
            .where(FamilyInvite.invite_code == invite.invite_code)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
        await async_session.execute(stmt)
        await async_session.commit()

        with pytest.raises(InviteExpiredError) as exc_info:
            await accept_invite(async_session, invite.invite_code, new_member.id)

        assert exc_info.value.status_code == 400
        assert exc_info.value.invite_code == invite.invite_code
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_already_member(async_session: AsyncSession):
    """Test that accepting invite when already member raises error."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Create invite BEFORE adding member (to avoid member limit issues)
        invite = await create_invite(async_session, aquarium.id, owner.id)

        # Add member manually (simulating they joined via another invite)
        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        # Try to accept invite - should fail because already a member
        with pytest.raises(AlreadyMemberError) as exc_info:
            await accept_invite(async_session, invite.invite_code, member.id)

        assert exc_info.value.status_code == 400
        assert exc_info.value.aquarium_id == aquarium.id
        assert exc_info.value.user_id == member.id
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_owner_cannot_accept_own_invite(async_session: AsyncSession):
    """Test that owner cannot accept their own invite."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        invite = await create_invite(async_session, aquarium.id, owner.id)

        # Owner is already a member, should raise AlreadyMemberError
        with pytest.raises(AlreadyMemberError):
            await accept_invite(async_session, invite.invite_code, owner.id)
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_used_invite_not_found(async_session: AsyncSession):
    """Test that already used invite cannot be accepted again."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member1 = await create_test_user(async_session, email="member1@example.com")
        member2 = await create_test_user(async_session, email="member2@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        invite = await create_invite(async_session, aquarium.id, owner.id)

        # First member accepts
        await accept_invite(async_session, invite.invite_code, member1.id)

        # Second member tries to use same invite - should not find it
        with pytest.raises(InviteNotFoundError):
            await accept_invite(async_session, invite.invite_code, member2.id)
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_accept_invite_member_limit_free(async_session: AsyncSession):
    """Test that free user limit is enforced on accept."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(
            async_session, email="owner@example.com", subscription_status="free"
        )
        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Create invite BEFORE reaching limit (owner counts as 1, limit is 2)
        new_member = await create_test_user(async_session, email="newmember@example.com")
        invite = await create_invite(async_session, aquarium.id, owner.id)

        # Now add members to reach limit
        for i in range(FREE_MEMBER_LIMIT - 1):
            member = await create_test_user(
                async_session, email=f"member{i}@example.com"
            )
            am = AquariumMember(
                aquarium_id=aquarium.id,
                user_id=member.id,
                role="member",
            )
            async_session.add(am)
        await async_session.commit()

        # Now at limit, try to accept invite - should fail
        with pytest.raises(MemberLimitExceededError) as exc_info:
            await accept_invite(async_session, invite.invite_code, new_member.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_family_data(async_session)


# remove_member tests


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_member_owner_can_remove(async_session: AsyncSession):
    """Test that owner can remove a member."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        # Verify member exists
        members_before = await get_family_members(async_session, aquarium.id, owner.id)
        assert len(members_before) == 2

        # Owner removes member
        await remove_member(async_session, aquarium.id, member.id, owner.id)

        # Verify member is removed
        members_after = await get_family_members(async_session, aquarium.id, owner.id)
        assert len(members_after) == 1
        assert members_after[0].user_id == owner.id
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_member_member_cannot_remove(async_session: AsyncSession):
    """Test that member cannot remove anyone."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member1 = await create_test_user(async_session, email="member1@example.com")
        member2 = await create_test_user(async_session, email="member2@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add members
        am1 = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member1.id,
            role="member",
        )
        am2 = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member2.id,
            role="member",
        )
        async_session.add_all([am1, am2])
        await async_session.commit()

        # Member1 tries to remove Member2 - should fail
        with pytest.raises(AquariumOwnerRequiredError):
            await remove_member(async_session, aquarium.id, member2.id, member1.id)
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_member_owner_cannot_remove_self(async_session: AsyncSession):
    """Test that owner cannot remove themselves."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Owner tries to remove themselves - should fail
        with pytest.raises(CannotRemoveOwnerError) as exc_info:
            await remove_member(async_session, aquarium.id, owner.id, owner.id)

        assert exc_info.value.status_code == 400
        assert exc_info.value.aquarium_id == aquarium.id
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_member_not_found(async_session: AsyncSession):
    """Test that removing non-existent member raises error."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        stranger = await create_test_user(async_session, email="stranger@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Try to remove user who is not a member
        with pytest.raises(MemberNotFoundError) as exc_info:
            await remove_member(async_session, aquarium.id, stranger.id, owner.id)

        assert exc_info.value.status_code == 404
        assert exc_info.value.aquarium_id == aquarium.id
        assert exc_info.value.user_id == stranger.id
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_member_non_member_cannot_access(async_session: AsyncSession):
    """Test that non-member cannot access remove_member."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")
        stranger = await create_test_user(async_session, email="stranger@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        # Stranger tries to remove member - should fail with access denied
        with pytest.raises(AquariumAccessDeniedError):
            await remove_member(async_session, aquarium.id, member.id, stranger.id)
    finally:
        await cleanup_family_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_member_after_removal_cannot_access(async_session: AsyncSession):
    """Test that removed member loses access to aquarium."""
    await cleanup_family_data(async_session)
    try:
        owner = await create_test_user(async_session, email="owner@example.com")
        member = await create_test_user(async_session, email="member@example.com")

        data = AquariumCreate(name="Test Aquarium")
        aquarium = await create_aquarium(async_session, owner.id, data)

        # Add member
        am = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member.id,
            role="member",
        )
        async_session.add(am)
        await async_session.commit()

        # Verify member has access
        await get_family_members(async_session, aquarium.id, member.id)

        # Remove member
        await remove_member(async_session, aquarium.id, member.id, owner.id)

        # Verify member no longer has access
        with pytest.raises(AquariumAccessDeniedError):
            await get_family_members(async_session, aquarium.id, member.id)
    finally:
        await cleanup_family_data(async_session)
