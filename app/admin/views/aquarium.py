"""Admin views for Aquarium, AquariumMember, and FamilyInvite models."""

from sqladmin import ModelView

from app.models.aquarium import Aquarium, AquariumMember, FamilyInvite


class AquariumAdmin(ModelView, model=Aquarium):
    """Aquarium admin view — full CRUD, shows owner email and fish count."""

    column_list = [
        Aquarium.id,
        Aquarium.name,
        "owner.email",
        Aquarium.created_at,
        Aquarium.deleted_at,
    ]
    column_searchable_list = [Aquarium.name]
    column_sortable_list = [Aquarium.name, Aquarium.created_at]

    name = "Aquarium"
    name_plural = "Aquariums"
    icon = "fa-solid fa-water"


class AquariumMemberAdmin(ModelView, model=AquariumMember):
    """AquariumMember admin view — read + delete only."""

    column_list = [
        AquariumMember.aquarium_id,
        AquariumMember.user_id,
        AquariumMember.role,
        AquariumMember.joined_at,
    ]

    can_create = False
    can_edit = False
    name = "Aquarium Member"
    name_plural = "Aquarium Members"
    icon = "fa-solid fa-people-group"


class FamilyInviteAdmin(ModelView, model=FamilyInvite):
    """FamilyInvite admin view — read-only."""

    column_list = [
        FamilyInvite.id,
        FamilyInvite.aquarium_id,
        FamilyInvite.invite_code,
        FamilyInvite.created_by,
        FamilyInvite.expires_at,
        FamilyInvite.used_by,
        FamilyInvite.used_at,
        FamilyInvite.created_at,
    ]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Family Invite"
    name_plural = "Family Invites"
    icon = "fa-solid fa-envelope"
