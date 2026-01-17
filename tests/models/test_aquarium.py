import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Aquarium, AquariumMember, FamilyInvite, User


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_creation(async_session):
    """Test basic Aquarium creation with owner."""
    user = User(
        email="aquarium_owner@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(
        owner_id=user.id,
        name="My First Aquarium",
    )
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    assert aquarium.id is not None
    assert isinstance(aquarium.id, uuid.UUID)
    assert aquarium.name == "My First Aquarium"
    assert aquarium.owner_id == user.id


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_has_timestamp_mixin(async_session):
    """Test that Aquarium has TimestampMixin columns."""
    user = User(
        email="aquarium_ts@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(
        owner_id=user.id,
        name="Timestamp Aquarium",
    )
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    assert aquarium.created_at is not None
    assert aquarium.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_has_soft_delete_mixin(async_session):
    """Test that Aquarium has SoftDeleteMixin columns."""
    user = User(
        email="aquarium_sd@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(
        owner_id=user.id,
        name="SoftDelete Aquarium",
    )
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    assert aquarium.deleted_at is None
    assert aquarium.is_deleted() is False


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_owner_relationship(async_session):
    """Test Aquarium -> Owner relationship."""
    user = User(
        email="owner_rel@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(
        owner_id=user.id,
        name="Relationship Aquarium",
    )
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    assert aquarium.owner.id == user.id
    assert aquarium.owner.email == "owner_rel@example.com"


@pytest.mark.asyncio(loop_scope="session")
async def test_user_owned_aquariums_relationship(async_session):
    """Test User -> owned_aquariums relationship."""
    user = User(
        email="multi_aquarium@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium1 = Aquarium(owner_id=user.id, name="Aquarium 1")
    aquarium2 = Aquarium(owner_id=user.id, name="Aquarium 2")
    async_session.add_all([aquarium1, aquarium2])
    await async_session.commit()

    # Reload user with owned_aquariums eagerly loaded
    result = await async_session.execute(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.owned_aquariums))
    )
    user = result.scalar_one()

    assert len(user.owned_aquariums) == 2
    names = {a.name for a in user.owned_aquariums}
    assert names == {"Aquarium 1", "Aquarium 2"}


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_member_creation(async_session):
    """Test AquariumMember with composite primary key."""
    owner = User(email="member_owner@example.com", password_hash="hash")
    member_user = User(email="member_user@example.com", password_hash="hash")
    async_session.add_all([owner, member_user])
    await async_session.commit()
    await async_session.refresh(owner)
    await async_session.refresh(member_user)

    aquarium = Aquarium(owner_id=owner.id, name="Family Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=member_user.id,
        role="member",
    )
    async_session.add(member)
    await async_session.commit()
    await async_session.refresh(member)

    assert member.aquarium_id == aquarium.id
    assert member.user_id == member_user.id
    assert member.role == "member"
    assert member.joined_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_member_default_role(async_session):
    """Test that AquariumMember role defaults to 'member'."""
    owner = User(email="default_role_owner@example.com", password_hash="hash")
    member_user = User(email="default_role_member@example.com", password_hash="hash")
    async_session.add_all([owner, member_user])
    await async_session.commit()
    await async_session.refresh(owner)
    await async_session.refresh(member_user)

    aquarium = Aquarium(owner_id=owner.id, name="Default Role Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=member_user.id,
    )
    async_session.add(member)
    await async_session.commit()
    await async_session.refresh(member)

    assert member.role == "member"


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_members_relationship(async_session):
    """Test Aquarium -> members relationship."""
    owner = User(email="members_rel_owner@example.com", password_hash="hash")
    member1 = User(email="members_rel_member1@example.com", password_hash="hash")
    member2 = User(email="members_rel_member2@example.com", password_hash="hash")
    async_session.add_all([owner, member1, member2])
    await async_session.commit()
    await async_session.refresh(owner)
    await async_session.refresh(member1)
    await async_session.refresh(member2)

    aquarium = Aquarium(owner_id=owner.id, name="Members Rel Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    am1 = AquariumMember(aquarium_id=aquarium.id, user_id=member1.id, role="owner")
    am2 = AquariumMember(aquarium_id=aquarium.id, user_id=member2.id, role="member")
    async_session.add_all([am1, am2])
    await async_session.commit()

    # Reload aquarium with members eagerly loaded
    result = await async_session.execute(
        select(Aquarium)
        .where(Aquarium.id == aquarium.id)
        .options(selectinload(Aquarium.members))
    )
    aquarium = result.scalar_one()

    assert len(aquarium.members) == 2
    roles = {m.role for m in aquarium.members}
    assert roles == {"owner", "member"}


@pytest.mark.asyncio(loop_scope="session")
async def test_user_aquarium_memberships_relationship(async_session):
    """Test User -> aquarium_memberships relationship."""
    owner = User(email="memberships_owner@example.com", password_hash="hash")
    member_user = User(email="memberships_member@example.com", password_hash="hash")
    async_session.add_all([owner, member_user])
    await async_session.commit()
    await async_session.refresh(owner)
    await async_session.refresh(member_user)

    aquarium1 = Aquarium(owner_id=owner.id, name="Memberships Aquarium 1")
    aquarium2 = Aquarium(owner_id=owner.id, name="Memberships Aquarium 2")
    async_session.add_all([aquarium1, aquarium2])
    await async_session.commit()
    await async_session.refresh(aquarium1)
    await async_session.refresh(aquarium2)

    am1 = AquariumMember(aquarium_id=aquarium1.id, user_id=member_user.id)
    am2 = AquariumMember(aquarium_id=aquarium2.id, user_id=member_user.id)
    async_session.add_all([am1, am2])
    await async_session.commit()

    # Reload member_user with aquarium_memberships eagerly loaded
    result = await async_session.execute(
        select(User)
        .where(User.id == member_user.id)
        .options(selectinload(User.aquarium_memberships))
    )
    member_user = result.scalar_one()

    assert len(member_user.aquarium_memberships) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_family_invite_creation(async_session):
    """Test FamilyInvite creation."""
    owner = User(email="invite_owner@example.com", password_hash="hash")
    async_session.add(owner)
    await async_session.commit()
    await async_session.refresh(owner)

    aquarium = Aquarium(owner_id=owner.id, name="Invite Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    invite = FamilyInvite(
        aquarium_id=aquarium.id,
        invite_code="ABC123XYZ",
        created_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    async_session.add(invite)
    await async_session.commit()
    await async_session.refresh(invite)

    assert invite.id is not None
    assert invite.invite_code == "ABC123XYZ"
    assert invite.used_by is None
    assert invite.used_at is None
    assert invite.created_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_family_invite_used(async_session):
    """Test FamilyInvite when used by another user."""
    owner = User(email="invite_used_owner@example.com", password_hash="hash")
    invited_user = User(email="invited_user@example.com", password_hash="hash")
    async_session.add_all([owner, invited_user])
    await async_session.commit()
    await async_session.refresh(owner)
    await async_session.refresh(invited_user)

    aquarium = Aquarium(owner_id=owner.id, name="Used Invite Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    invite = FamilyInvite(
        aquarium_id=aquarium.id,
        invite_code="USED123CODE",
        created_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    async_session.add(invite)
    await async_session.commit()
    await async_session.refresh(invite)

    invite.used_by = invited_user.id
    invite.used_at = datetime.now(UTC)
    await async_session.commit()
    await async_session.refresh(invite)

    assert invite.used_by == invited_user.id
    assert invite.used_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_family_invite_relationships(async_session):
    """Test FamilyInvite relationships."""
    owner = User(email="invite_rel_owner@example.com", password_hash="hash")
    async_session.add(owner)
    await async_session.commit()
    await async_session.refresh(owner)

    aquarium = Aquarium(owner_id=owner.id, name="Rel Invite Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    invite = FamilyInvite(
        aquarium_id=aquarium.id,
        invite_code="REL123CODE",
        created_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    async_session.add(invite)
    await async_session.commit()
    await async_session.refresh(invite)

    assert invite.aquarium.id == aquarium.id
    assert invite.creator.id == owner.id


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_family_invites_relationship(async_session):
    """Test Aquarium -> family_invites relationship."""
    owner = User(email="aquarium_invites_owner@example.com", password_hash="hash")
    async_session.add(owner)
    await async_session.commit()
    await async_session.refresh(owner)

    aquarium = Aquarium(owner_id=owner.id, name="Multi Invite Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    invite1 = FamilyInvite(
        aquarium_id=aquarium.id,
        invite_code="MULTI1CODE",
        created_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    invite2 = FamilyInvite(
        aquarium_id=aquarium.id,
        invite_code="MULTI2CODE",
        created_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    async_session.add_all([invite1, invite2])
    await async_session.commit()

    # Reload aquarium with family_invites eagerly loaded
    result = await async_session.execute(
        select(Aquarium)
        .where(Aquarium.id == aquarium.id)
        .options(selectinload(Aquarium.family_invites))
    )
    aquarium = result.scalar_one()

    assert len(aquarium.family_invites) == 2
    codes = {i.invite_code for i in aquarium.family_invites}
    assert codes == {"MULTI1CODE", "MULTI2CODE"}


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_cascade_delete_members(async_session):
    """Test that deleting Aquarium cascades to AquariumMember."""
    owner = User(email="cascade_member_owner@example.com", password_hash="hash")
    member_user = User(email="cascade_member_user@example.com", password_hash="hash")
    async_session.add_all([owner, member_user])
    await async_session.commit()
    await async_session.refresh(owner)
    await async_session.refresh(member_user)

    aquarium = Aquarium(owner_id=owner.id, name="Cascade Member Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    aquarium_id = aquarium.id
    member = AquariumMember(aquarium_id=aquarium.id, user_id=member_user.id)
    async_session.add(member)
    await async_session.commit()

    await async_session.delete(aquarium)
    await async_session.commit()

    deleted_member = await async_session.get(
        AquariumMember, (aquarium_id, member_user.id)
    )
    assert deleted_member is None


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_cascade_delete_invites(async_session):
    """Test that deleting Aquarium cascades to FamilyInvite."""
    owner = User(email="cascade_invite_owner@example.com", password_hash="hash")
    async_session.add(owner)
    await async_session.commit()
    await async_session.refresh(owner)

    aquarium = Aquarium(owner_id=owner.id, name="Cascade Invite Aquarium")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    invite = FamilyInvite(
        aquarium_id=aquarium.id,
        invite_code="CASCADEINVITE",
        created_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    async_session.add(invite)
    await async_session.commit()
    invite_id = invite.id

    await async_session.delete(aquarium)
    await async_session.commit()

    deleted_invite = await async_session.get(FamilyInvite, invite_id)
    assert deleted_invite is None
