"""Tests for Family Mode Pydantic schemas."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.family import (
    AcceptInviteRequest,
    CreateInviteResponse,
    FamilyListResponse,
    FamilyMemberResponse,
    InviteResponse,
)


class TestFamilyMemberResponse:
    """Tests for FamilyMemberResponse schema."""

    def test_valid_owner_member(self):
        """Test valid family member with owner role."""
        user_id = uuid4()
        joined = datetime.now(UTC)

        response = FamilyMemberResponse(
            user_id=user_id,
            nickname="John",
            avatar_key="avatars/550e8400-e29b-41d4-a716-446655440000/a1b2c3d4.webp",
            role="owner",
            joined_at=joined,
        )

        assert response.user_id == user_id
        assert response.nickname == "John"
        assert response.avatar_key == "avatars/550e8400-e29b-41d4-a716-446655440000/a1b2c3d4.webp"
        assert response.role == "owner"
        assert response.joined_at == joined

    def test_valid_member_role(self):
        """Test valid family member with member role."""
        response = FamilyMemberResponse(
            user_id=uuid4(),
            role="member",
            joined_at=datetime.now(UTC),
        )
        assert response.role == "member"

    def test_invalid_role(self):
        """Test that invalid role is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FamilyMemberResponse(
                user_id=uuid4(),
                role="admin",
                joined_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("role",)

    def test_optional_nickname(self):
        """Test that nickname is optional."""
        response = FamilyMemberResponse(
            user_id=uuid4(),
            role="member",
            joined_at=datetime.now(UTC),
        )
        assert response.nickname is None

    def test_optional_avatar_key(self):
        """Test that avatar_key is optional."""
        response = FamilyMemberResponse(
            user_id=uuid4(),
            role="member",
            joined_at=datetime.now(UTC),
        )
        assert response.avatar_key is None

    def test_from_attributes(self):
        """Test FamilyMemberResponse can be created from ORM-like object."""

        class MockMember:
            def __init__(self):
                self.user_id = uuid4()
                self.nickname = "TestUser"
                self.avatar_key = None
                self.role = "member"
                self.joined_at = datetime.now(UTC)

        mock = MockMember()
        response = FamilyMemberResponse.model_validate(mock)

        assert response.user_id == mock.user_id
        assert response.nickname == mock.nickname
        assert response.role == mock.role


class TestInviteResponse:
    """Tests for InviteResponse schema."""

    def test_valid_invite(self):
        """Test valid invite response."""
        expires = datetime.now(UTC)

        response = InviteResponse(
            invite_code="ABC12345",
            invite_link="fishfeed://invite/ABC12345",
            expires_at=expires,
        )

        assert response.invite_code == "ABC12345"
        assert response.invite_link == "fishfeed://invite/ABC12345"
        assert response.expires_at == expires

    def test_invite_code_exactly_8_chars(self):
        """Test that invite_code must be exactly 8 characters."""
        response = InviteResponse(
            invite_code="12345678",
            invite_link="fishfeed://invite/12345678",
            expires_at=datetime.now(UTC),
        )
        assert len(response.invite_code) == 8

    def test_invite_code_too_short(self):
        """Test that invite_code shorter than 8 characters is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            InviteResponse(
                invite_code="ABC123",
                invite_link="fishfeed://invite/ABC123",
                expires_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("invite_code",)
        assert "string_too_short" in errors[0]["type"]

    def test_invite_code_too_long(self):
        """Test that invite_code longer than 8 characters is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            InviteResponse(
                invite_code="ABC123456789",
                invite_link="fishfeed://invite/ABC123456789",
                expires_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("invite_code",)
        assert "string_too_long" in errors[0]["type"]


class TestCreateInviteResponse:
    """Tests for CreateInviteResponse schema."""

    def test_valid_create_invite(self):
        """Test valid create invite response."""
        aquarium_id = uuid4()
        expires = datetime.now(UTC)

        response = CreateInviteResponse(
            invite_code="ABCD1234",
            invite_link="fishfeed://invite/ABCD1234",
            expires_at=expires,
            aquarium_id=aquarium_id,
        )

        assert response.invite_code == "ABCD1234"
        assert response.aquarium_id == aquarium_id

    def test_inherits_invite_response_validation(self):
        """Test that CreateInviteResponse inherits InviteResponse validation."""
        with pytest.raises(ValidationError) as exc_info:
            CreateInviteResponse(
                invite_code="SHORT",
                invite_link="fishfeed://invite/SHORT",
                expires_at=datetime.now(UTC),
                aquarium_id=uuid4(),
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("invite_code",) for e in errors)


class TestAcceptInviteRequest:
    """Tests for AcceptInviteRequest schema."""

    def test_valid_accept_invite(self):
        """Test valid accept invite request."""
        request = AcceptInviteRequest(invite_code="ABC12345")
        assert request.invite_code == "ABC12345"

    def test_invite_code_alphanumeric(self):
        """Test that invite_code accepts alphanumeric characters."""
        request = AcceptInviteRequest(invite_code="AbC12345")
        assert request.invite_code == "AbC12345"

    def test_invite_code_with_url_safe_chars(self):
        """Test that invite_code accepts URL-safe characters (alphanumeric, -, _)."""
        # Hyphen is allowed
        request = AcceptInviteRequest(invite_code="ABC-1234")
        assert request.invite_code == "ABC-1234"

        # Underscore is allowed
        request = AcceptInviteRequest(invite_code="ABC_1234")
        assert request.invite_code == "ABC_1234"

    def test_invite_code_with_invalid_chars(self):
        """Test that invite_code with invalid special characters is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AcceptInviteRequest(invite_code="ABC@1234")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("invite_code",)
        assert "url-safe" in str(errors[0]["msg"]).lower()

    def test_invite_code_empty(self):
        """Test that empty invite_code is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AcceptInviteRequest(invite_code="")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("invite_code",)

    def test_invite_code_max_length(self):
        """Test that invite_code respects max length of 32."""
        long_code = "A" * 32
        request = AcceptInviteRequest(invite_code=long_code)
        assert len(request.invite_code) == 32

    def test_invite_code_too_long(self):
        """Test that invite_code longer than 32 characters is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AcceptInviteRequest(invite_code="A" * 33)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("invite_code",)


class TestFamilyListResponse:
    """Tests for FamilyListResponse schema."""

    def test_valid_family_list(self):
        """Test valid family list response."""
        aquarium_id = uuid4()
        member = FamilyMemberResponse(
            user_id=uuid4(),
            nickname="User1",
            role="owner",
            joined_at=datetime.now(UTC),
        )

        response = FamilyListResponse(
            aquarium_id=aquarium_id,
            members=[member],
        )

        assert response.aquarium_id == aquarium_id
        assert len(response.members) == 1
        assert response.members[0].nickname == "User1"

    def test_empty_members_list(self):
        """Test family list with no members."""
        response = FamilyListResponse(
            aquarium_id=uuid4(),
            members=[],
        )
        assert len(response.members) == 0

    def test_default_empty_members(self):
        """Test that members defaults to empty list."""
        response = FamilyListResponse(aquarium_id=uuid4())
        assert response.members == []

    def test_multiple_members(self):
        """Test family list with multiple members."""
        members = [
            FamilyMemberResponse(
                user_id=uuid4(),
                nickname="Owner",
                role="owner",
                joined_at=datetime.now(UTC),
            ),
            FamilyMemberResponse(
                user_id=uuid4(),
                nickname="Member1",
                role="member",
                joined_at=datetime.now(UTC),
            ),
            FamilyMemberResponse(
                user_id=uuid4(),
                nickname="Member2",
                role="member",
                joined_at=datetime.now(UTC),
            ),
        ]

        response = FamilyListResponse(
            aquarium_id=uuid4(),
            members=members,
        )

        assert len(response.members) == 3
        assert response.members[0].role == "owner"
        assert response.members[1].role == "member"

    def test_serialization_to_dict(self):
        """Test FamilyListResponse serializes to dict correctly."""
        aquarium_id = uuid4()
        user_id = uuid4()
        joined = datetime.now(UTC)

        response = FamilyListResponse(
            aquarium_id=aquarium_id,
            members=[
                FamilyMemberResponse(
                    user_id=user_id,
                    nickname="Test",
                    role="owner",
                    joined_at=joined,
                )
            ],
        )

        data = response.model_dump()

        assert data["aquarium_id"] == aquarium_id
        assert len(data["members"]) == 1
        assert data["members"][0]["user_id"] == user_id
        assert data["members"][0]["role"] == "owner"
