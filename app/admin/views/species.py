"""Admin view for Species model."""

import io
import logging
from typing import Any

import wtforms
from markupsafe import Markup
from PIL import Image
from sqladmin import ModelView
from starlette.requests import Request

from app.admin.views._image_utils import presigned_url_sync, upload_species_image
from app.models.species import Species

logger = logging.getLogger(__name__)

SPECIES_MAX_DIMENSION = 512
SPECIES_WEBP_QUALITY = 85


def _species_image_thumbnail(model: object, name: str) -> Markup:
    """Render a 48px thumbnail for the species list view."""
    key = getattr(model, "image_url", None)
    if not key:
        return Markup('<span style="color:#999">—</span>')
    url = presigned_url_sync(key)
    if not url:
        return Markup('<span style="color:#999">—</span>')
    return Markup(
        f'<img src="{url}" width="48" height="48" '
        f'style="object-fit:cover;border-radius:4px" />'
    )


def _species_image_detail(model: object, name: str) -> Markup:
    """Render a larger image preview for the species detail view."""
    key = getattr(model, "image_url", None)
    if not key:
        return Markup('<span style="color:#999">No image</span>')
    url = presigned_url_sync(key)
    if not url:
        return Markup('<span style="color:#999">No image</span>')
    return Markup(
        f'<a href="{url}" target="_blank">'
        f'<img src="{url}" width="256" height="256" '
        f'style="object-fit:cover;border-radius:8px" />'
        f"</a>"
    )


def _convert_to_webp(raw_bytes: bytes) -> bytes:
    """Convert image bytes to WebP, resize to fit within max dimension."""
    img: Image.Image = Image.open(io.BytesIO(raw_bytes))

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    w, h = img.size
    max_dim = SPECIES_MAX_DIMENSION
    if w > max_dim or h > max_dim:
        if w >= h:
            new_w = max_dim
            new_h = int(max_dim * h / w)
        else:
            new_h = max_dim
            new_w = int(max_dim * w / h)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=SPECIES_WEBP_QUALITY)
    return buf.getvalue()


class SpeciesAdmin(ModelView, model=Species):
    """Species admin view — full CRUD for reference data."""

    column_list = [
        Species.id,
        Species.image_url,
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
    column_labels = {Species.image_url: "Image"}
    column_formatters = {Species.image_url: _species_image_thumbnail}  # type: ignore[dict-item]
    column_formatters_detail = {Species.image_url: _species_image_detail}  # type: ignore[dict-item]

    # Show file upload widget instead of text input for image_url.
    form_overrides = {"image_url": wtforms.FileField}
    form_args = {"image_url": {"label": "Image (upload to replace)"}}
    form_include_pk = True

    name = "Species"
    name_plural = "Species"
    icon = "fa-solid fa-fish"

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request,
    ) -> None:
        """Process uploaded image file before saving the model.

        If a file is uploaded: convert to WebP, upload to S3, set image_url
        to the S3 key. If no file is uploaded: preserve the existing value.
        """
        uploaded = data.get("image_url")
        file_bytes = b""

        # Extract bytes from the uploaded file (UploadFile or FileStorage).
        if uploaded:
            if hasattr(uploaded, "read"):
                raw = uploaded.read()
                # Handle both sync and async read().
                if hasattr(raw, "__await__"):
                    raw = await raw
                file_bytes = raw if isinstance(raw, bytes) else b""

        if file_bytes:
            species_id = data.get("id") or getattr(model, "id", None)
            if not species_id:
                raise ValueError("Species ID is required for image upload")

            webp_bytes = _convert_to_webp(file_bytes)
            s3_key = f"species/{species_id}/photo.webp"
            upload_species_image(s3_key, webp_bytes)
            data["image_url"] = s3_key
            logger.info("Uploaded species image: %s (%d bytes)", s3_key, len(webp_bytes))
        else:
            # No file uploaded — preserve existing image_url (don't overwrite with None).
            if not is_created:
                data["image_url"] = getattr(model, "image_url", None)
            else:
                data["image_url"] = None
