"""Admin analytics endpoints for cleanup and anonymization management."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.dependencies import CurrentAdmin
from app.jobs.analytics_cleanup import (
    analytics_cleanup_job,
    anonymize_old_events_job,
    delete_old_events_job,
)

router = APIRouter(prefix="/analytics", tags=["admin-analytics"])


class CleanupResponse(BaseModel):
    """Response for cleanup job trigger."""

    message: str
    job: str
    dry_run: bool
    status: str


class CleanupResultResponse(BaseModel):
    """Response with cleanup job results."""

    job: str
    dry_run: bool
    cutoff_date: str
    total_processed: int
    batches_processed: int


class AnalyticsCleanupResponse(BaseModel):
    """Response for full analytics cleanup job."""

    message: str
    dry_run: bool
    anonymization: dict
    retention: dict


@router.post(
    "/anonymize",
    response_model=CleanupResultResponse,
    summary="Trigger analytics anonymization",
    description="Manually trigger anonymization of analytics events older than configured threshold. Admin only.",
)
async def trigger_anonymization(
    admin: CurrentAdmin,
    dry_run: Annotated[
        bool,
        Query(description="If true, only report what would be done without making changes"),
    ] = False,
) -> CleanupResultResponse:
    """Trigger manual anonymization of old analytics events.

    This endpoint allows admins to manually trigger the anonymization job
    that normally runs daily. Events older than ANALYTICS_ANONYMIZE_AFTER_DAYS
    will have their user_id set to NULL, ip_hash set to 'anonymized',
    and PII removed from properties.

    Args:
        admin: Authenticated admin user.
        dry_run: If True, only report what would be done.

    Returns:
        Job execution results.
    """
    result = await anonymize_old_events_job(dry_run=dry_run)

    return CleanupResultResponse(
        job="anonymize_old_events",
        dry_run=dry_run,
        cutoff_date=result["cutoff_date"],
        total_processed=result["total_anonymized"] if not dry_run else result["would_anonymize"],
        batches_processed=result["batches_processed"],
    )


@router.post(
    "/cleanup",
    response_model=CleanupResultResponse,
    summary="Trigger analytics retention cleanup",
    description="Manually trigger deletion of analytics events older than retention period. Admin only.",
)
async def trigger_retention_cleanup(
    admin: CurrentAdmin,
    dry_run: Annotated[
        bool,
        Query(description="If true, only report what would be done without making changes"),
    ] = False,
) -> CleanupResultResponse:
    """Trigger manual deletion of old analytics events.

    This endpoint allows admins to manually trigger the retention job
    that normally runs daily. Events older than ANALYTICS_RETENTION_DAYS
    will be permanently deleted.

    Args:
        admin: Authenticated admin user.
        dry_run: If True, only report what would be done.

    Returns:
        Job execution results.
    """
    result = await delete_old_events_job(dry_run=dry_run)

    return CleanupResultResponse(
        job="delete_old_events",
        dry_run=dry_run,
        cutoff_date=result["cutoff_date"],
        total_processed=result["total_deleted"] if not dry_run else result["would_delete"],
        batches_processed=result["batches_processed"],
    )


@router.post(
    "/full-cleanup",
    response_model=AnalyticsCleanupResponse,
    summary="Trigger full analytics cleanup",
    description="Manually trigger both anonymization and retention cleanup. Admin only.",
)
async def trigger_full_cleanup(
    admin: CurrentAdmin,
    dry_run: Annotated[
        bool,
        Query(description="If true, only report what would be done without making changes"),
    ] = False,
) -> AnalyticsCleanupResponse:
    """Trigger both anonymization and retention cleanup jobs.

    This endpoint runs the full analytics cleanup job that normally
    runs daily at 3:00 UTC.

    Args:
        admin: Authenticated admin user.
        dry_run: If True, only report what would be done.

    Returns:
        Combined job execution results.
    """
    result = await analytics_cleanup_job(dry_run=dry_run)

    return AnalyticsCleanupResponse(
        message="Analytics cleanup completed" if not dry_run else "Dry-run completed",
        dry_run=dry_run,
        anonymization=result["anonymization"],
        retention=result["retention"],
    )
