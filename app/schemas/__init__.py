"""Pydantic schemas package."""

from app.schemas.ai import (
    AlternativeSpecies,
    ScanConfirmRequest,
    ScanRequest,
    ScanResponse,
    ScansRemainingResponse,
)
from app.schemas.aquarium import (
    AquariumBase,
    AquariumCreate,
    AquariumResponse,
    AquariumUpdate,
    AquariumWithFish,
)
from app.schemas.auth import (
    LoginRequest,
    OAuthRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.family import (
    AcceptInviteRequest,
    CreateInviteResponse,
    FamilyListResponse,
    FamilyMemberResponse,
    InviteResponse,
)
from app.schemas.feeding import (
    EventResponse,
    FeedingStatus,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
    TodayEventsResponse,
)
from app.schemas.fish import (
    AddedVia,
    FishCreate,
    FishResponse,
    FishUpdate,
)
from app.schemas.gamification import (
    AchievementResponse,
    AchievementType,
    ShareAchievementRequest,
    StreakResponse,
    UserStatsResponse,
)
from app.schemas.notification import (
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
    PlatformType,
    PushTokenRequest,
    PushTokenResponse,
)
from app.schemas.purchase import (
    EnvironmentType,
    RestorePurchaseRequest,
    StoreType,
    SubscriptionStatus,
    SubscriptionStatusType,
    WebhookEvent,
    WebhookEventData,
    WebhookEventType,
    WebhookResponse,
)
from app.schemas.purchase import (
    PlatformType as PurchasePlatformType,
)
from app.schemas.species import (
    CareLevel,
    FoodType,
    SpeciesCreate,
    SpeciesListResponse,
    SpeciesResponse,
    SpeciesSearchQuery,
    SpeciesUpdate,
    WaterType,
)
from app.schemas.sync import (
    ChangeItem,
    ConflictItem,
    EntityType,
    OperationType,
    ServerState,
    SyncRequest,
    SyncResponse,
)

__all__ = [
    # AI
    "AlternativeSpecies",
    "ScanConfirmRequest",
    "ScanRequest",
    "ScanResponse",
    "ScansRemainingResponse",
    # Aquarium
    "AquariumBase",
    "AquariumCreate",
    "AquariumResponse",
    "AquariumUpdate",
    "AquariumWithFish",
    # Family
    "AcceptInviteRequest",
    "CreateInviteResponse",
    "FamilyListResponse",
    "FamilyMemberResponse",
    "InviteResponse",
    # Auth
    "LoginRequest",
    "OAuthRequest",
    "PasswordChangeRequest",
    "PasswordResetRequest",
    "RefreshRequest",
    "RegisterRequest",
    "TokenResponse",
    "UserResponse",
    # Feeding
    "EventResponse",
    "FeedingStatus",
    "ScheduleCreate",
    "ScheduleResponse",
    "ScheduleUpdate",
    "TodayEventsResponse",
    # Fish
    "AddedVia",
    "FishCreate",
    "FishResponse",
    "FishUpdate",
    # Notification
    "NotificationPreferencesResponse",
    "NotificationPreferencesUpdate",
    "PlatformType",
    "PushTokenRequest",
    "PushTokenResponse",
    # Species
    "CareLevel",
    "FoodType",
    "SpeciesCreate",
    "SpeciesListResponse",
    "SpeciesResponse",
    "SpeciesSearchQuery",
    "SpeciesUpdate",
    "WaterType",
    # Sync
    "ChangeItem",
    "ConflictItem",
    "EntityType",
    "OperationType",
    "ServerState",
    "SyncRequest",
    "SyncResponse",
    # Gamification
    "AchievementResponse",
    "AchievementType",
    "ShareAchievementRequest",
    "StreakResponse",
    "UserStatsResponse",
    # Purchase
    "EnvironmentType",
    "PurchasePlatformType",
    "RestorePurchaseRequest",
    "StoreType",
    "SubscriptionStatus",
    "SubscriptionStatusType",
    "WebhookEvent",
    "WebhookEventData",
    "WebhookEventType",
    "WebhookResponse",
]

# Rebuild models with forward references after all imports are complete
AquariumWithFish.model_rebuild()
