"""Admin view for Species model."""

from sqladmin import ModelView

from app.models.species import Species


class SpeciesAdmin(ModelView, model=Species):
    """Species admin view — full CRUD for reference data."""

    column_list = [
        Species.id,
        Species.common_name,
        Species.scientific_name,
        Species.care_level,
        Species.water_type,
        Species.feeding_frequency,
        Species.created_at,
    ]
    column_searchable_list = [Species.common_name, Species.scientific_name]
    column_sortable_list = [
        Species.common_name,
        Species.care_level,
        Species.water_type,
        Species.created_at,
    ]

    name = "Species"
    name_plural = "Species"
    icon = "fa-solid fa-fish"
