"""Admin view for AIScan model."""

from sqladmin import ModelView

from app.models.ai import AIScan


class AIScanAdmin(ModelView, model=AIScan):
    """AIScan admin view — read-only."""

    column_list = [
        AIScan.id,
        AIScan.user_id,
        AIScan.detected_species_id,
        AIScan.confidence,
        AIScan.confirmed_species_id,
        AIScan.was_corrected,
        AIScan.processing_time_ms,
        AIScan.created_at,
    ]
    column_sortable_list = [AIScan.confidence, AIScan.created_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "AI Scan"
    name_plural = "AI Scans"
    icon = "fa-solid fa-robot"
