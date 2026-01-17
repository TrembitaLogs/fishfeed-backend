"""User API endpoints for GDPR compliance."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.analytics import DataExportResponse
from app.services.analytics import (
    GDPRError,
    UserNotFoundError,
    delete_user_data,
    export_user_data,
)
from app.services.storage import StorageNotConfiguredError

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me/data-export",
    response_model=DataExportResponse,
    status_code=status.HTTP_200_OK,
    summary="Export user data (GDPR)",
    responses={
        200: {"description": "Data export URL generated successfully"},
        401: {"description": "Not authenticated"},
        503: {"description": "Storage service not configured"},
    },
)
async def get_data_export(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> DataExportResponse:
    """Export all user data as JSON for GDPR compliance.

    Collects all data associated with the authenticated user from all tables,
    generates a JSON file, uploads it to S3, and returns a presigned download URL.

    The download URL is valid for 24 hours.
    """
    try:
        return await export_user_data(db, current_user.id)
    except StorageNotConfiguredError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service is not configured",
        ) from None
    except GDPRError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        ) from None


@router.delete(
    "/me/data",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete all user data (GDPR)",
    responses={
        204: {"description": "All user data deleted successfully"},
        401: {"description": "Not authenticated"},
        404: {"description": "User not found"},
    },
)
async def delete_all_data(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Permanently delete all user data for GDPR compliance.

    This is a hard delete operation that removes all data associated
    with the authenticated user from all database tables. This action
    is irreversible.

    After deletion, the user will be logged out and unable to access
    the service with their current credentials.
    """
    try:
        await delete_user_data(db, current_user.id)
    except UserNotFoundError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        ) from None
    except GDPRError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        ) from None
