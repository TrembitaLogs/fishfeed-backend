#!/usr/bin/env python3
"""
Download species images from fishfeed.club and upload to MinIO/S3.

Usage:
    uv run python scripts/seed_species_images.py [--dry-run]

The script:
1. Fetches the list of images from https://fishfeed.club/images/
2. Matches them to species in the database by common_name / scientific_name
3. Downloads matched images
4. Uploads to S3 (MinIO) as WebP under species/{species_id}/photo.webp
5. Updates species.image_url in the database with the S3 key

Environment variables (from .env):
    DATABASE_URL - PostgreSQL connection string
    S3_ENDPOINT_URL - S3/MinIO endpoint (e.g., http://localhost:9000)
    S3_ACCESS_KEY - S3 access key
    S3_SECRET_KEY - S3 secret key
    S3_IMAGES_BUCKET_NAME - Bucket name (e.g., fishfeed-images)
"""

import asyncio
import io
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import boto3
from PIL import Image

# Load .env manually (avoid adding python-dotenv as dependency)
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value

# Config
SOURCE_URL = "https://fishfeed.club/images/"
DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fishfeed"
)
S3_ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
# For local script, use localhost instead of Docker internal hostname
if "minio:9000" in S3_ENDPOINT:
    S3_ENDPOINT = S3_ENDPOINT.replace("minio:9000", "localhost:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.environ.get("S3_IMAGES_BUCKET_NAME", "fishfeed-images")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")

# Parse asyncpg-compatible DSN from SQLAlchemy URL
DSN = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

WEBP_QUALITY = 85
MAX_DIMENSION = 512  # Max width/height for species thumbnails

# Manual overrides for ambiguous matches (image_path -> species_id).
# Used when multiple species share the same common_name or scientific_name.
MANUAL_OVERRIDES: dict[str, str] = {
    "/images/German Blue Ram — Mikrogeophagus ramirezi.jpg": "german-blue-ram",
    "/images/Mikrogeophagus ramirezi.jpg": "ram-cichlid",
    "/images/Carassius auratus.jpg": "goldfish",
    "/images/Carassius_auratus.jpg": "goldfish-fantail",
}


def fetch_image_list() -> list[str]:
    """Fetch list of image paths from fishfeed.club/images/."""
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "FishFeed-Seeder"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode()

    paths = re.findall(r'href="([^"]*\.(?:jpg|jpeg|png|webp))"', html, re.IGNORECASE)
    return [urllib.parse.unquote(p) for p in paths]


def parse_image_name(path: str) -> tuple[str | None, str | None]:
    """Extract common_name and scientific_name from image filename.

    Handles patterns:
        "Common Name — Scientific Name.jpg"
        "Common Name - Scientific Name.jpg"
        "Scientific_name.jpg" / "Scientific name.jpg"
    """
    filename = path.rsplit("/", 1)[-1]
    name = filename.rsplit(".", 1)[0]  # Remove extension

    # Pattern 1: "Common Name — Scientific Name" (em-dash)
    if " — " in name:
        parts = name.split(" — ", 1)
        return parts[0].strip(), parts[1].strip()

    # Pattern 2: "Common Name - Scientific Name" (hyphen with spaces)
    if " - " in name:
        parts = name.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()

    # Pattern 3: "Scientific_name" or "Scientific name" (no separator)
    # Replace underscores with spaces
    cleaned = name.replace("_", " ").strip()
    return None, cleaned


def build_matching_index(
    species_rows: list[tuple[str, str, str | None]],
) -> dict[str, str]:
    """Build lookup dicts from species data.

    Returns a dict mapping normalized name -> species_id.
    Keys include: common_name, scientific_name, and variations.
    """
    index: dict[str, str] = {}

    for sid, common_name, scientific_name in species_rows:
        # Index by common_name (lowercase)
        cn_lower = common_name.lower().strip()
        index[cn_lower] = sid

        # Index by scientific_name (lowercase)
        if scientific_name:
            sn_lower = scientific_name.lower().strip()
            index[sn_lower] = sid

    return index


def match_image_to_species(
    path: str, index: dict[str, str]
) -> str | None:
    """Try to match an image path to a species ID."""
    # Check manual overrides first
    if path in MANUAL_OVERRIDES:
        return MANUAL_OVERRIDES[path]

    common, scientific = parse_image_name(path)

    # Try exact common_name match
    if common:
        key = common.lower().strip()
        if key in index:
            return index[key]

    # Try exact scientific_name match
    if scientific:
        key = scientific.lower().strip()
        if key in index:
            return index[key]

    # Try scientific with "var." removed (e.g., "Danio rerio var. frankei")
    if scientific and "var." in scientific.lower():
        key = re.sub(r"\s*var\.\s*\w+", "", scientific, flags=re.IGNORECASE).lower().strip()
        if key in index:
            return index[key]

    return None


def download_image(path: str) -> bytes:
    """Download image from fishfeed.club."""
    # Ensure path starts with /
    clean_path = path if path.startswith("/") else f"/{path}"
    url = f"https://fishfeed.club{urllib.parse.quote(clean_path, safe='/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "FishFeed-Seeder"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def convert_to_webp(image_bytes: bytes) -> bytes:
    """Convert image to WebP, resize if needed."""
    img = Image.open(io.BytesIO(image_bytes))

    # Convert to RGB if necessary (e.g., RGBA PNG)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize to fit within MAX_DIMENSION box
    w, h = img.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        if w >= h:
            new_w = MAX_DIMENSION
            new_h = int(MAX_DIMENSION * h / w)
        else:
            new_h = MAX_DIMENSION
            new_w = int(MAX_DIMENSION * w / h)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue()


def upload_to_s3(s3_client: object, key: str, data: bytes) -> None:
    """Upload bytes to S3/MinIO."""
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=data,
        ContentType="image/webp",
    )


async def update_database(species_keys: dict[str, str]) -> None:
    """Update species.image_url in the database."""
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        # Clear all existing image_url values first
        await conn.execute("UPDATE species SET image_url = NULL")
        print("  Cleared all existing image_url values")

        # Update matched species
        for species_id, s3_key in species_keys.items():
            await conn.execute(
                "UPDATE species SET image_url = $1 WHERE id = $2",
                s3_key,
                species_id,
            )
        print(f"  Updated {len(species_keys)} species with S3 keys")
    finally:
        await conn.close()


async def get_species_list() -> list[tuple[str, str, str | None]]:
    """Fetch all species from the database."""
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, common_name, scientific_name FROM species ORDER BY id"
        )
        return [(r["id"], r["common_name"], r["scientific_name"]) for r in rows]
    finally:
        await conn.close()


async def main() -> None:
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("Species Image Seeder")
    print("=" * 60)
    if dry_run:
        print("  [DRY RUN] No changes will be made\n")

    # 1. Fetch species from DB
    print("1. Fetching species from database...")
    species_rows = await get_species_list()
    print(f"   Found {len(species_rows)} species\n")

    # 2. Fetch image list from fishfeed.club
    print("2. Fetching image list from fishfeed.club...")
    image_paths = fetch_image_list()
    print(f"   Found {len(image_paths)} images\n")

    # 3. Build matching index
    index = build_matching_index(species_rows)

    # 4. Match images to species
    print("3. Matching images to species...")
    matches: dict[str, str] = {}  # species_id -> image_path
    unmatched: list[str] = []
    duplicates: list[tuple[str, str, str]] = []  # (species_id, old_path, new_path)

    for path in image_paths:
        species_id = match_image_to_species(path, index)
        if species_id:
            if species_id in matches:
                # Prefer "Common Name — Scientific Name" format over bare scientific name
                old_path = matches[species_id]
                old_has_separator = " — " in old_path or " - " in old_path
                new_has_separator = " — " in path or " - " in path

                if new_has_separator and not old_has_separator:
                    duplicates.append((species_id, old_path, path))
                    matches[species_id] = path
                else:
                    duplicates.append((species_id, path, old_path))
            else:
                matches[species_id] = path
        else:
            unmatched.append(path)

    # Species without images
    matched_ids = set(matches.keys())
    all_ids = {r[0] for r in species_rows}
    missing = all_ids - matched_ids

    print(f"   Matched: {len(matches)}")
    print(f"   Unmatched images: {len(unmatched)}")
    print(f"   Duplicate matches (resolved): {len(duplicates)}")
    print(f"   Species without images: {len(missing)}")

    if unmatched:
        print("\n   Unmatched images:")
        for p in sorted(unmatched):
            print(f"     - {p}")

    if missing:
        missing_names = {r[0]: r[1] for r in species_rows}
        print("\n   Species without images:")
        for sid in sorted(missing):
            print(f"     - {sid} ({missing_names.get(sid, '?')})")

    if dry_run:
        print("\n[DRY RUN] Would process:")
        for sid, path in sorted(matches.items()):
            s3_key = f"species/{sid}/photo.webp"
            print(f"  {path} -> {s3_key}")
        print("\n[DRY RUN] Done. No changes made.")
        return

    # 5. Download, convert, upload
    print(f"\n4. Downloading, converting, and uploading {len(matches)} images...")

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
    )

    species_keys: dict[str, str] = {}  # species_id -> s3_key
    errors: list[tuple[str, str]] = []

    for i, (species_id, path) in enumerate(sorted(matches.items()), 1):
        s3_key = f"species/{species_id}/photo.webp"
        try:
            # Download
            raw = download_image(path)

            # Convert to WebP
            webp_data = convert_to_webp(raw)

            # Upload
            upload_to_s3(s3, s3_key, webp_data)

            species_keys[species_id] = s3_key
            orig_kb = len(raw) / 1024
            webp_kb = len(webp_data) / 1024
            print(f"   [{i}/{len(matches)}] {species_id}: {orig_kb:.0f}KB -> {webp_kb:.0f}KB ({s3_key})")
        except Exception as e:
            errors.append((species_id, str(e)))
            print(f"   [{i}/{len(matches)}] {species_id}: ERROR - {e}")

    # 6. Update database
    print("\n5. Updating database...")
    await update_database(species_keys)

    # Summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  Uploaded: {len(species_keys)}")
    print(f"  Errors: {len(errors)}")
    print(f"  Species without images: {len(missing)}")
    if errors:
        print("\n  Failed uploads:")
        for sid, err in errors:
            print(f"    - {sid}: {err}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
