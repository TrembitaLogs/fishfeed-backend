"""Admin view for WebhookTransaction model."""

from sqladmin import ModelView

from app.models.purchase import WebhookTransaction


class WebhookTransactionAdmin(ModelView, model=WebhookTransaction):
    """WebhookTransaction admin view — read-only."""

    column_list = [
        WebhookTransaction.id,
        WebhookTransaction.transaction_id,
        WebhookTransaction.event_type,
        WebhookTransaction.user_id,
        WebhookTransaction.processing_result,
        WebhookTransaction.processed_at,
        WebhookTransaction.correlation_id,
    ]
    column_searchable_list = [WebhookTransaction.transaction_id, WebhookTransaction.event_type]
    column_sortable_list = [WebhookTransaction.event_type, WebhookTransaction.processed_at]

    can_create = False
    can_edit = False
    can_delete = False
    name = "Webhook Transaction"
    name_plural = "Webhook Transactions"
    icon = "fa-solid fa-credit-card"
