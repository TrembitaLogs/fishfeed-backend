"""Background jobs for garbage collection of orphaned entity images.

Orphaned images accumulate when users replace or remove photos from
entities (aquariums, fish, avatars). The old S3 keys are stored in
the ``orphaned_images`` table with a grace period before deletion.

This module provides:
- ``image_cleanup_job()``: Daily job — delete orphaned images older than 7 days
- ``s3_reconciliation_job()``: Weekly job — find unreferenced S3 objects and
  register them as orphaned (safety net for edge cases)

Usage:
    # Via APScheduler (registered in feeding_worker.py)
    # Daily at 4:00 UTC / Weekly on Sunday at 5:00 UTC

    # Via CLI for testing
    python -m app.workers.feeding_worker --run-once --job=image_cleanup
    python -m app.workers.feeding_worker --run-once --job=s3_reconciliation
"""

from datetime import UTC, datetime, timedelta

import aioboto3
import structlog
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import async_session_maker
from app.models.aquarium import Aquarium
from app.models.fish import Fish
from app.models.orphaned_image import OrphanedImage
from app.models.user import User

logger = structlog.get_logger(__name__)
settings = get_settings()

# Grace period before deleting orphaned images from S3 (days).
ORPHAN_GRACE_PERIOD_DAYS = 7

# Maximum number of orphaned images to process per DB query batch.
CLEANUP_BATCH_SIZE = 100

# S3 listing page size for reconciliation (max 1000 per AWS API).
RECONCILIATION_S3_PAGE_SIZE = 1000

# Batch size for inserting new orphaned records during reconciliation.
RECONCILIATION_INSERT_BATCH_SIZE = 100

# S3 key prefix → entity_type mapping for reconciliation.
_PREFIX_TO_ENTITY_TYPE: dict[str, str] = {
    "aquariums/": "aquarium",
    "fish/": "fish",
    "avatars/": "avatar",
}

# Prefixes to scan during reconciliation.
S3_PREFIXES = list(_PREFIX_TO_ENTITY_TYPE.keys())


async def image_cleanup_job() -> dict:
    """Delete orphaned images older than grace period from S3 and database.

    Processes orphaned images in batches:

    1. Query ``orphaned_images`` where ``orphaned_at < now() - 7 days``
    2. For each record, attempt to delete the S3 object
    3. Only remove DB records where S3 deletion succeeded (partial failure)
    4. If **all** S3 deletes in a batch fail, stop processing (S3 likely
       unreachable) — remaining records will be retried on the next run

    Returns:
        Dict with job statistics (deleted counts, failures, batches).
    """
    # Bail out early if S3 is not configured
    if not settings.S3_ENDPOINT_URL or not settings.S3_ACCESS_KEY or not settings.S3_SECRET_KEY:
        logger.warning("S3 not configured, skipping image cleanup")
        return {
            "job": "image_cleanup",
            "skipped": True,
            "reason": "S3 not configured",
        }

    cutoff = datetime.now(UTC) - timedelta(days=ORPHAN_GRACE_PERIOD_DAYS)

    logger.info(
        "Starting image_cleanup_job",
        cutoff=cutoff.isoformat(),
        grace_period_days=ORPHAN_GRACE_PERIOD_DAYS,
    )

    total_deleted_s3 = 0
    total_failed_s3 = 0
    total_deleted_db = 0
    total_batches = 0

    s3_session = aioboto3.Session()
    s3_config = {
        "service_name": "s3",
        "endpoint_url": settings.S3_ENDPOINT_URL,
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
        "region_name": settings.S3_REGION,
    }

    async with async_session_maker() as db:
        while True:
            # 1. Fetch next batch of orphaned images past grace period
            stmt = (
                select(OrphanedImage)
                .where(OrphanedImage.orphaned_at < cutoff)
                .order_by(OrphanedImage.orphaned_at)
                .limit(CLEANUP_BATCH_SIZE)
            )
            result = await db.execute(stmt)
            orphans = list(result.scalars().all())

            if not orphans:
                break

            total_batches += 1
            succeeded_ids: list = []

            # 2. Delete each object from S3
            async with s3_session.client(**s3_config) as s3:
                for orphan in orphans:
                    try:
                        await s3.delete_object(
                            Bucket=settings.S3_IMAGES_BUCKET_NAME,
                            Key=orphan.old_key,
                        )
                        succeeded_ids.append(orphan.id)
                        total_deleted_s3 += 1
                    except Exception:
                        total_failed_s3 += 1
                        logger.exception(
                            "failed_to_delete_orphaned_image",
                            key=orphan.old_key,
                            entity_type=orphan.entity_type,
                        )

            # 3. Remove successfully-deleted records from the database
            if succeeded_ids:
                await db.execute(
                    delete(OrphanedImage).where(
                        OrphanedImage.id.in_(succeeded_ids),
                    )
                )
                await db.commit()
                total_deleted_db += len(succeeded_ids)

            logger.info(
                "image_cleanup_batch_processed",
                batch=total_batches,
                succeeded=len(succeeded_ids),
                failed=len(orphans) - len(succeeded_ids),
            )

            # 4. If every S3 delete in this batch failed, stop —
            #    S3 is likely unreachable and retrying won't help.
            if not succeeded_ids:
                logger.warning(
                    "All S3 deletes failed in batch, stopping early",
                    batch_size=len(orphans),
                )
                break

    stats = {
        "job": "image_cleanup",
        "cutoff": cutoff.isoformat(),
        "grace_period_days": ORPHAN_GRACE_PERIOD_DAYS,
        "total_deleted_s3": total_deleted_s3,
        "total_failed_s3": total_failed_s3,
        "total_deleted_db": total_deleted_db,
        "batches_processed": total_batches,
    }

    logger.info("image_cleanup_job completed", **stats)
    return stats


def _entity_type_from_key(key: str) -> str:
    """Derive entity_type from the S3 object key prefix."""
    for prefix, entity_type in _PREFIX_TO_ENTITY_TYPE.items():
        if key.startswith(prefix):
            return entity_type
    return "unknown"


async def s3_reconciliation_job() -> dict:
    """Find unreferenced S3 objects and register them as orphaned.

    Weekly safety-net that covers edge cases where files end up in S3
    without being tracked in ``orphaned_images``:

    - Crash after upload but before DB update
    - Concurrent upload where LWW loser's file is never recorded

    Algorithm:

    1. Collect all ``photo_key``/``avatar_key`` from aquariums, fish, users
       (**including soft-deleted** entities — their photos must not be orphaned)
    2. Collect all ``old_key`` values already tracked in ``orphaned_images``
    3. Paginate through S3 bucket for each known prefix (streaming — keys
       are checked against the set page-by-page, not accumulated in memory)
    4. Keys present in S3 but absent from both sets → insert into
       ``orphaned_images`` with ``orphaned_at = now()``

    Returns:
        Dict with job statistics.
    """
    if not settings.S3_ENDPOINT_URL or not settings.S3_ACCESS_KEY or not settings.S3_SECRET_KEY:
        logger.warning("S3 not configured, skipping S3 reconciliation")
        return {
            "job": "s3_reconciliation",
            "skipped": True,
            "reason": "S3 not configured",
        }

    logger.info("Starting s3_reconciliation_job")

    # --- Step 1 & 2: Build set of all known keys ----------------------
    known_keys: set[str] = set()

    async with async_session_maker() as db:
        # Aquarium photo_keys (including soft-deleted)
        result = await db.execute(
            select(Aquarium.photo_key).where(Aquarium.photo_key.isnot(None))
        )
        known_keys.update(row[0] for row in result.all())

        # Fish photo_keys (including soft-deleted)
        result = await db.execute(
            select(Fish.photo_key).where(Fish.photo_key.isnot(None))
        )
        known_keys.update(row[0] for row in result.all())

        # User avatar_keys
        result = await db.execute(
            select(User.avatar_key).where(User.avatar_key.isnot(None))
        )
        known_keys.update(row[0] for row in result.all())

        # Already-tracked orphaned keys
        result = await db.execute(select(OrphanedImage.old_key))
        known_keys.update(row[0] for row in result.all())

    known_count = len(known_keys)

    # --- Step 3 & 4: Stream S3 listing and detect unreferenced ---------
    s3_session = aioboto3.Session()
    s3_config = {
        "service_name": "s3",
        "endpoint_url": settings.S3_ENDPOINT_URL,
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
        "region_name": settings.S3_REGION,
    }

    total_s3_objects = 0
    new_orphaned = 0
    now = datetime.now(UTC)

    # Collect unreferenced keys per page and flush in batches
    pending: list[OrphanedImage] = []

    async with s3_session.client(**s3_config) as s3:
        for prefix in S3_PREFIXES:
            continuation_token: str | None = None

            while True:
                kwargs: dict[str, str | int] = {
                    "Bucket": settings.S3_IMAGES_BUCKET_NAME,
                    "Prefix": prefix,
                    "MaxKeys": RECONCILIATION_S3_PAGE_SIZE,
                }
                if continuation_token:
                    kwargs["ContinuationToken"] = continuation_token

                response = await s3.list_objects_v2(**kwargs)

                for obj in response.get("Contents", []):
                    key = obj["Key"]
                    total_s3_objects += 1

                    if key not in known_keys:
                        pending.append(
                            OrphanedImage(
                                old_key=key,
                                entity_type=_entity_type_from_key(key),
                                orphaned_at=now,
                            )
                        )

                # Flush pending orphans in batches
                if len(pending) >= RECONCILIATION_INSERT_BATCH_SIZE:
                    async with async_session_maker() as db:
                        db.add_all(pending)
                        await db.commit()
                    new_orphaned += len(pending)
                    logger.info(
                        "s3_reconciliation_batch_flushed",
                        count=len(pending),
                        total_so_far=new_orphaned,
                    )
                    pending.clear()

                if response.get("IsTruncated"):
                    continuation_token = response["NextContinuationToken"]
                else:
                    break

    # Flush remaining orphans
    if pending:
        async with async_session_maker() as db:
            db.add_all(pending)
            await db.commit()
        new_orphaned += len(pending)

    stats = {
        "job": "s3_reconciliation",
        "known_keys_in_db": known_count,
        "total_s3_objects": total_s3_objects,
        "new_orphaned": new_orphaned,
    }

    logger.info("s3_reconciliation_job completed", **stats)
    return stats
