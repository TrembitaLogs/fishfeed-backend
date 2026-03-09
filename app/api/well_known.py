"""Well-known endpoints for Android App Links and iOS Universal Links."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_settings

router = APIRouter(prefix="/.well-known", tags=["well-known"])


@router.get("/assetlinks.json")
async def android_asset_links() -> JSONResponse:
    """Android App Links verification.

    Serves Digital Asset Links JSON for verifying the app can handle
    https://fishfeed.club deep links.
    """
    settings = get_settings()
    return JSONResponse(
        content=[
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": "com.fishfeed.fishfeed",
                    "sha256_cert_fingerprints": settings.APP_LINK_FINGERPRINTS,
                },
            },
        ],
        media_type="application/json",
    )


@router.get("/apple-app-site-association")
async def apple_app_site_association() -> JSONResponse:
    """iOS Universal Links verification.

    Serves Apple App Site Association JSON for verifying the app can handle
    https://fishfeed.club deep links.
    """
    settings = get_settings()
    return JSONResponse(
        content={
            "applinks": {
                "apps": [],
                "details": [
                    {
                        "appID": f"{settings.APPLE_TEAM_ID}.com.fishfeed.fishfeed",
                        "paths": ["/join/*"],
                    },
                ],
            },
        },
        media_type="application/json",
    )
