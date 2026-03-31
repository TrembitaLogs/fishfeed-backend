#!/usr/bin/env python3
"""Seed script for populating the database with fish species data.

Usage:
    python scripts/seed_species.py              # Normal run
    python scripts/seed_species.py --dry-run    # Preview changes without DB modifications
    python scripts/seed_species.py --clear-existing  # Clear existing species before seeding
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import async_session_maker, engine
from app.models.species import Species

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
DATA_FILE = Path(__file__).parent.parent / "data" / "species.json"
BATCH_SIZE = 50


def load_species_data() -> list[dict]:
    """Load species data from JSON file."""
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Species data file not found: {DATA_FILE}")

    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)

    logger.info("Loaded %d species from %s", len(data), DATA_FILE)
    return data


def validate_species(species: dict) -> list[str]:
    """Validate a species record and return list of errors."""
    errors = []
    required_fields = ["id", "common_name", "food_types", "feeding_frequency"]

    for field in required_fields:
        if field not in species or species[field] is None:
            errors.append(f"Missing required field: {field}")

    # Validate food_types values
    valid_food_types = {"flakes", "pellets", "frozen", "live", "vegetables", "algae"}
    if "food_types" in species and species["food_types"]:
        invalid_types = set(species["food_types"]) - valid_food_types
        if invalid_types:
            errors.append(f"Invalid food_types: {invalid_types}")

    # Validate care_level
    valid_care_levels = {"beginner", "intermediate", "advanced"}
    if species.get("care_level") and species["care_level"] not in valid_care_levels:
        errors.append(f"Invalid care_level: {species['care_level']}")

    # Validate water_type
    valid_water_types = {"freshwater", "saltwater", "brackish"}
    if species.get("water_type") and species["water_type"] not in valid_water_types:
        errors.append(f"Invalid water_type: {species['water_type']}")

    return errors


async def clear_existing_species(session: AsyncSession) -> int:
    """Delete all existing species records."""
    result = await session.execute(delete(Species))
    count = result.rowcount
    await session.commit()
    logger.info("Cleared %d existing species records", count)
    return count


async def get_existing_species_ids(session: AsyncSession) -> set[str]:
    """Get IDs of all existing species."""
    result = await session.execute(select(Species.id))
    return {row[0] for row in result.fetchall()}


async def upsert_species_batch(
    session: AsyncSession,
    species_batch: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Upsert a batch of species records.

    Returns tuple of (inserted_count, updated_count).
    """
    if dry_run:
        return len(species_batch), 0

    # Prepare data for upsert
    values = []
    for species in species_batch:
        values.append(
            {
                "id": species["id"],
                "common_name": species["common_name"],
                "scientific_name": species.get("scientific_name"),
                "image_url": species.get("image_url"),
                "food_types": species.get("food_types", []),
                "feeding_frequency": species.get("feeding_frequency", 2),
                "portion_hint": species.get("portion_hint"),
                "care_level": species.get("care_level", "beginner"),
                "water_type": species.get("water_type", "freshwater"),
                "metadata_": species.get("metadata", {}),
            }
        )

    # PostgreSQL upsert (INSERT ... ON CONFLICT DO UPDATE)
    # For image_url: only overwrite if the new value is not null (COALESCE),
    # so seed_species_images.py values are preserved.
    stmt = insert(Species).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "common_name": stmt.excluded.common_name,
            "scientific_name": stmt.excluded.scientific_name,
            "image_url": func.coalesce(stmt.excluded.image_url, Species.image_url),
            "food_types": stmt.excluded.food_types,
            "feeding_frequency": stmt.excluded.feeding_frequency,
            "portion_hint": stmt.excluded.portion_hint,
            "care_level": stmt.excluded.care_level,
            "water_type": stmt.excluded.water_type,
            "metadata": stmt.excluded.metadata,  # DB column name is "metadata"
        },
    )

    await session.execute(stmt)
    await session.commit()

    return len(species_batch), 0


async def seed_species(
    dry_run: bool = False,
    clear_existing: bool = False,
) -> dict:
    """Main function to seed species data.

    Args:
        dry_run: If True, only validate and preview changes without DB modifications.
        clear_existing: If True, delete all existing species before seeding.

    Returns:
        Dictionary with statistics about the seeding operation.
    """
    stats = {
        "total_in_file": 0,
        "valid": 0,
        "invalid": 0,
        "inserted": 0,
        "updated": 0,
        "cleared": 0,
        "errors": [],
    }

    # Load data
    try:
        species_data = load_species_data()
        stats["total_in_file"] = len(species_data)
    except FileNotFoundError as e:
        logger.error(str(e))
        stats["errors"].append(str(e))
        return stats

    # Validate all records
    valid_species = []
    for species in species_data:
        errors = validate_species(species)
        if errors:
            stats["invalid"] += 1
            error_msg = f"Species '{species.get('id', 'unknown')}': {', '.join(errors)}"
            stats["errors"].append(error_msg)
            logger.warning(error_msg)
        else:
            stats["valid"] += 1
            valid_species.append(species)

    logger.info("Validated %d species, %d invalid", stats['valid'], stats['invalid'])

    if dry_run:
        logger.info("DRY RUN: No database changes will be made")
        popular_count = sum(
            1 for s in valid_species if s.get("metadata", {}).get("is_popular", False)
        )
        logger.info("Would insert/update %d species", stats['valid'])
        logger.info("Popular species: %d", popular_count)

        # Show sample of species
        logger.info("Sample species (first 5):")
        for species in valid_species[:5]:
            logger.info("  - %s: %s", species['id'], species['common_name'])

        stats["inserted"] = stats["valid"]
        return stats

    # Database operations
    async with async_session_maker() as session:
        # Clear existing if requested
        if clear_existing:
            stats["cleared"] = await clear_existing_species(session)

        # Get existing IDs for tracking inserts vs updates
        existing_ids = await get_existing_species_ids(session)

        # Process in batches
        total_processed = 0
        for i in range(0, len(valid_species), BATCH_SIZE):
            batch = valid_species[i : i + BATCH_SIZE]
            inserted, updated = await upsert_species_batch(session, batch, dry_run)

            # Count actual inserts vs updates based on existing IDs
            for species in batch:
                if species["id"] in existing_ids:
                    stats["updated"] += 1
                else:
                    stats["inserted"] += 1

            total_processed += len(batch)
            logger.info("Progress: %d/%d species", total_processed, len(valid_species))

    logger.info(
        "Seeding complete: %d inserted, %d updated", stats['inserted'], stats['updated']
    )
    return stats


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Seed fish species data into the database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/seed_species.py              # Normal run
    python scripts/seed_species.py --dry-run    # Preview without changes
    python scripts/seed_species.py --clear-existing  # Clear and reseed
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying the database",
    )
    parser.add_argument(
        "--clear-existing",
        action="store_true",
        help="Delete all existing species before seeding",
    )
    return parser.parse_args()


async def main() -> int:
    """Main entry point."""
    args = parse_args()

    logger.info("=" * 60)
    logger.info("Fish Species Seed Script")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("Mode: DRY RUN (no database changes)")
    elif args.clear_existing:
        logger.info("Mode: CLEAR AND RESEED")
    else:
        logger.info("Mode: UPSERT (insert new, update existing)")

    try:
        stats = await seed_species(
            dry_run=args.dry_run,
            clear_existing=args.clear_existing,
        )

        logger.info("=" * 60)
        logger.info("Summary:")
        logger.info("  Total in file: %d", stats['total_in_file'])
        logger.info("  Valid records: %d", stats['valid'])
        logger.info("  Invalid records: %d", stats['invalid'])
        if stats["cleared"]:
            logger.info("  Cleared: %d", stats['cleared'])
        logger.info("  Inserted: %d", stats['inserted'])
        logger.info("  Updated: %d", stats['updated'])
        logger.info("=" * 60)

        if stats["errors"]:
            logger.warning("Completed with %d validation errors", len(stats['errors']))
            return 1

        logger.info("Seeding completed successfully!")
        return 0

    except Exception as e:
        logger.exception("Seeding failed: %s", e)
        return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
