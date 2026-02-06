"""Admin view for Fish model."""

from sqladmin import ModelView

from app.models.fish import Fish


class FishAdmin(ModelView, model=Fish):
    """Fish admin view — full CRUD, shows aquarium name and species name."""

    column_list = [
        Fish.id,
        "aquarium.name",
        "species.common_name",
        Fish.custom_name,
        Fish.quantity,
        Fish.added_via,
        Fish.created_at,
        Fish.deleted_at,
    ]
    column_searchable_list = [Fish.custom_name]
    column_sortable_list = [Fish.quantity, Fish.created_at]

    name = "Fish"
    name_plural = "Fish"
    icon = "fa-solid fa-fish-fins"
