"""E2E tests for family API endpoints."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


async def register_and_login(
    client: AsyncClient,
    email: str,
) -> dict:
    """Helper to register and login a user, returns tokens."""
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123"},
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "SecurePass123"},
    )
    return response.json()


def auth_headers(tokens: dict) -> dict:
    """Helper to create auth headers."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.asyncio(loop_scope="session")
class TestListFamilyMembers:
    """Tests for GET /aquariums/{id}/family endpoint."""

    async def test_list_family_returns_owner(self, client: AsyncClient):
        """Test that listing family returns the owner."""
        email = f"family-list-owner-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Family Tank"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # List family
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["aquarium_id"] == aquarium_id
        assert len(data["members"]) == 1
        assert data["members"][0]["role"] == "owner"

    async def test_list_family_without_auth_returns_401(self, client: AsyncClient):
        """Test that listing family without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.get(f"/api/v1/aquariums/{random_id}/family")
        assert response.status_code == 401

    async def test_list_family_nonexistent_aquarium_returns_404(
        self, client: AsyncClient
    ):
        """Test that listing family for non-existent aquarium returns 404."""
        email = f"family-list-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.get(
            f"/api/v1/aquariums/{random_id}/family",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_list_family_other_user_returns_403(self, client: AsyncClient):
        """Test that non-member cannot list family."""
        email1 = f"family-owner-{uuid.uuid4()}@example.com"
        email2 = f"family-stranger-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # User 1 creates aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Private Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # User 2 tries to list family
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403

    async def test_member_can_list_family(self, client: AsyncClient):
        """Test that member can list family members."""
        email1 = f"family-owner-{uuid.uuid4()}@example.com"
        email2 = f"family-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Shared Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create invite
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        # Member accepts invite
        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # Member lists family
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["members"]) == 2


@pytest.mark.asyncio(loop_scope="session")
class TestCreateInvite:
    """Tests for POST /aquariums/{id}/family/invite endpoint."""

    async def test_create_invite_returns_201(self, client: AsyncClient):
        """Test that creating invite returns 201."""
        email = f"invite-create-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Invite Tank"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Create invite
        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        data = response.json()
        assert "invite_code" in data
        assert len(data["invite_code"]) == 8
        assert "invite_link" in data
        assert "expires_at" in data

    async def test_create_invite_without_auth_returns_401(self, client: AsyncClient):
        """Test that creating invite without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.post(f"/api/v1/aquariums/{random_id}/family/invite")
        assert response.status_code == 401

    async def test_member_cannot_create_invite(self, client: AsyncClient):
        """Test that member cannot create invite."""
        email1 = f"invite-owner-{uuid.uuid4()}@example.com"
        email2 = f"invite-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Shared Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create and accept invite for member
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # Member tries to create invite
        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403

    async def test_create_invite_free_limit_exceeded(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that free user cannot create invite when limit exceeded."""
        email1 = f"invite-free-limit-{uuid.uuid4()}@example.com"
        email2 = f"invite-member1-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Free Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create and accept first invite (now at limit: owner + 1 member = 2)
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # Try to create another invite - should fail
        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )

        assert response.status_code == 403
        assert "limit exceeded" in response.json()["detail"].lower()

    async def test_create_invite_premium_higher_limit(
        self, client: AsyncClient, async_engine
    ):
        """Test that premium user has higher member limit."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        email1 = f"invite-premium-{uuid.uuid4()}@example.com"
        email2 = f"invite-member1-{uuid.uuid4()}@example.com"
        email3 = f"invite-member2-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)
        tokens3 = await register_and_login(client, email3)

        # Set premium subscription using a fresh session from same engine
        session_maker = async_sessionmaker(async_engine, class_=AsyncSession)
        async with session_maker() as session:
            await session.execute(
                text(
                    "UPDATE users SET subscription_status = 'premium' WHERE email = :email"
                ),
                {"email": email1},
            )
            await session.commit()

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Premium Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create and accept first invite
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        assert invite_response.status_code == 201
        invite_code = invite_response.json()["invite_code"]
        accept1 = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )
        assert accept1.status_code == 200

        # Create and accept second invite (would exceed free limit)
        invite_response2 = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        assert invite_response2.status_code == 201

        invite_code2 = invite_response2.json()["invite_code"]
        accept2 = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code2},
            headers=auth_headers(tokens3),
        )
        assert accept2.status_code == 200

        # Verify 3 members now
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens1),
        )
        assert len(response.json()["members"]) == 3


@pytest.mark.asyncio(loop_scope="session")
class TestAcceptInvite:
    """Tests for POST /family/accept endpoint."""

    async def test_accept_invite_returns_aquarium(self, client: AsyncClient):
        """Test that accepting invite returns the aquarium."""
        email1 = f"accept-owner-{uuid.uuid4()}@example.com"
        email2 = f"accept-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Accept Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create invite
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        # Accept invite
        response = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == aquarium_id
        assert data["name"] == "Accept Tank"

    async def test_accept_invite_without_auth_returns_401(self, client: AsyncClient):
        """Test that accepting invite without auth returns 401."""
        response = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": "testcode"},
        )
        assert response.status_code == 401

    async def test_accept_invite_not_found_returns_404(self, client: AsyncClient):
        """Test that accepting non-existent invite returns 404."""
        email = f"accept-notfound-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": "nonexist"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_accept_invite_already_member_returns_400(
        self, client: AsyncClient, async_engine
    ):
        """Test that accepting invite when already member returns 400."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        email1 = f"accept-already-owner-{uuid.uuid4()}@example.com"
        email2 = f"accept-already-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Set premium subscription to allow multiple invites
        session_maker = async_sessionmaker(async_engine, class_=AsyncSession)
        async with session_maker() as session:
            await session.execute(
                text(
                    "UPDATE users SET subscription_status = 'premium' WHERE email = :email"
                ),
                {"email": email1},
            )
            await session.commit()

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Already Member Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create and accept first invite
        invite_response1 = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        assert invite_response1.status_code == 201
        invite_code1 = invite_response1.json()["invite_code"]

        accept1 = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code1},
            headers=auth_headers(tokens2),
        )
        assert accept1.status_code == 200

        # Create second invite
        invite_response2 = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        assert invite_response2.status_code == 201
        invite_code2 = invite_response2.json()["invite_code"]

        # Try to accept again - should fail because already a member
        response = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code2},
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 400
        assert "already" in response.json()["detail"].lower()

    async def test_accept_invite_member_sees_aquarium(self, client: AsyncClient):
        """Test that member can see aquarium after accepting invite."""
        email1 = f"accept-see-owner-{uuid.uuid4()}@example.com"
        email2 = f"accept-see-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Visible Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Before invite, member cannot see aquarium
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 403

        # Create and accept invite
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # After invite, member can see aquarium
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 200

    async def test_accept_invite_member_appears_in_list(self, client: AsyncClient):
        """Test that aquarium appears in member's list after joining."""
        email1 = f"accept-list-owner-{uuid.uuid4()}@example.com"
        email2 = f"accept-list-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Listed Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Before invite, member doesn't see aquarium in list
        response = await client.get("/api/v1/aquariums", headers=auth_headers(tokens2))
        aquarium_ids = [a["id"] for a in response.json()]
        assert aquarium_id not in aquarium_ids

        # Create and accept invite
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # After invite, member sees aquarium in list
        response = await client.get("/api/v1/aquariums", headers=auth_headers(tokens2))
        aquarium_ids = [a["id"] for a in response.json()]
        assert aquarium_id in aquarium_ids


@pytest.mark.asyncio(loop_scope="session")
class TestRemoveMember:
    """Tests for DELETE /aquariums/{id}/family/{user_id} endpoint."""

    async def test_remove_member_returns_204(self, client: AsyncClient):
        """Test that removing member returns 204."""
        email1 = f"remove-owner-{uuid.uuid4()}@example.com"
        email2 = f"remove-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Remove Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Create and accept invite
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]

        accept_response = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )
        # Get member's user_id from family list
        family_response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens1),
        )
        members = family_response.json()["members"]
        member_user_id = next(m["user_id"] for m in members if m["role"] == "member")

        # Remove member
        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/family/{member_user_id}",
            headers=auth_headers(tokens1),
        )

        assert response.status_code == 204

    async def test_remove_member_without_auth_returns_401(self, client: AsyncClient):
        """Test that removing member without auth returns 401."""
        random_aq_id = str(uuid.uuid4())
        random_user_id = str(uuid.uuid4())

        response = await client.delete(
            f"/api/v1/aquariums/{random_aq_id}/family/{random_user_id}"
        )
        assert response.status_code == 401

    async def test_member_can_leave(self, client: AsyncClient):
        """Test that member can remove themselves (leave aquarium)."""
        email1 = f"remove-owner-{uuid.uuid4()}@example.com"
        email2 = f"remove-member1-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Leave Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Add member
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]
        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # Get member's user_id
        family_response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens1),
        )
        members = family_response.json()["members"]
        member_user_id = next(m["user_id"] for m in members if m["role"] == "member")

        # Member removes themselves (leave) - should succeed
        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/family/{member_user_id}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 204

    async def test_member_cannot_remove_others(self, client: AsyncClient, async_engine):
        """Test that member cannot remove other members."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        email1 = f"remove-owner2-{uuid.uuid4()}@example.com"
        email2 = f"remove-member2a-{uuid.uuid4()}@example.com"
        email3 = f"remove-member2b-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)
        tokens3 = await register_and_login(client, email3)

        # Set premium to allow 3 members
        session_maker = async_sessionmaker(async_engine, class_=AsyncSession)
        async with session_maker() as session:
            await session.execute(
                text(
                    "UPDATE users SET subscription_status = 'premium' WHERE email = :email"
                ),
                {"email": email1},
            )
            await session.commit()

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "No Remove Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Add first member
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]
        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # Add second member
        invite_response2 = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code2 = invite_response2.json()["invite_code"]
        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code2},
            headers=auth_headers(tokens3),
        )

        # Get member2's user_id
        family_response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens1),
        )
        members = family_response.json()["members"]
        member_user_ids = [m["user_id"] for m in members if m["role"] == "member"]

        # Member1 tries to remove member2 - should fail
        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/family/{member_user_ids[1]}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403

    async def test_owner_cannot_remove_self(self, client: AsyncClient):
        """Test that owner cannot remove themselves."""
        email = f"remove-self-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Self Remove Tank"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Get owner's user_id
        family_response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens),
        )
        owner_user_id = family_response.json()["members"][0]["user_id"]

        # Owner tries to remove themselves
        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/family/{owner_user_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 400

    async def test_remove_member_loses_access(self, client: AsyncClient):
        """Test that removed member loses access to aquarium."""
        email1 = f"remove-access-owner-{uuid.uuid4()}@example.com"
        email2 = f"remove-access-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Access Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Add member
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        assert invite_response.status_code == 201
        invite_code = invite_response.json()["invite_code"]

        accept_response = await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )
        assert accept_response.status_code == 200

        # Get member's user_id
        family_response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/family",
            headers=auth_headers(tokens1),
        )
        assert family_response.status_code == 200
        members = family_response.json()["members"]
        assert len(members) == 2, f"Expected 2 members, got {len(members)}: {members}"
        member_user_id = next(m["user_id"] for m in members if m["role"] == "member")

        # Verify member has access
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 200

        # Remove member
        remove_response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/family/{member_user_id}",
            headers=auth_headers(tokens1),
        )
        assert remove_response.status_code == 204

        # Verify member no longer has access
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 403

    async def test_remove_nonexistent_member_returns_404(self, client: AsyncClient):
        """Test that removing non-existent member returns 404."""
        email = f"remove-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "404 Tank"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Try to remove non-existent user
        random_user_id = str(uuid.uuid4())
        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/family/{random_user_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestMemberCannotDeleteAquarium:
    """Test that member cannot delete aquarium."""

    async def test_member_cannot_delete_aquarium(self, client: AsyncClient):
        """Test that member cannot delete aquarium (only owner can)."""
        email1 = f"delete-owner-{uuid.uuid4()}@example.com"
        email2 = f"delete-member-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "No Delete Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # Add member
        invite_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/family/invite",
            headers=auth_headers(tokens1),
        )
        invite_code = invite_response.json()["invite_code"]
        await client.post(
            "/api/v1/family/accept",
            json={"invite_code": invite_code},
            headers=auth_headers(tokens2),
        )

        # Member tries to delete aquarium
        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403
