from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    APP_NAME: str = "FishFeed API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"

    # Sentry
    SENTRY_DSN: str | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/fishfeed"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET_KEY: str = "your-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # OAuth
    GOOGLE_CLIENT_ID: str | None = None
    APPLE_CLIENT_ID: str | None = None

    # Family Mode
    INVITE_BASE_URL: str = "fishfeed://invite"

    # AI Image Processing
    MAX_IMAGE_SIZE_MB: int = 10
    AI_IMAGE_SIZE: int = 512

    # AI Provider Configuration
    AI_PROVIDER: str = "google_vision"  # google_vision | replicate
    GOOGLE_CLOUD_PROJECT: str | None = None
    GOOGLE_APPLICATION_CREDENTIALS: str | None = None
    REPLICATE_API_TOKEN: str | None = None
    AI_MIN_CONFIDENCE_THRESHOLD: float = 0.5
    AI_REQUEST_TIMEOUT_SECONDS: int = 30

    # Rate Limiting
    REDIS_KEY_PREFIX: str = "fishfeed:"
    FREE_USER_HOURLY_SCAN_LIMIT: int = 10

    # Global Rate Limiting (middleware)
    RATE_LIMIT_USER_PER_MIN: int = 100  # Per authenticated user
    RATE_LIMIT_IP_PER_MIN: int = 1000  # Per IP address
    RATE_LIMIT_ANALYTICS_EVENTS_PER_MIN: int = 50  # /analytics/events endpoint
    RATE_LIMIT_ANALYTICS_BATCH_PER_MIN: int = 10  # /analytics/events/batch endpoint
    RATE_LIMIT_WINDOW_SECONDS: int = 60  # Sliding window size
    RATE_LIMIT_ENABLED: bool = True  # Enable/disable rate limiting

    # Request limits (Slowloris protection)
    MAX_REQUEST_BODY_SIZE_MB: int = 10  # Max request body size
    REQUEST_TIMEOUT_SECONDS: int = 30  # Request processing timeout

    # S3 Storage (Hetzner Object Storage compatible)
    S3_ENDPOINT_URL: str | None = None
    S3_ACCESS_KEY: str | None = None
    S3_SECRET_KEY: str | None = None
    S3_BUCKET_NAME: str = "fishfeed-scans"
    S3_REGION: str = "eu-central"
    S3_RETENTION_DAYS: int = 30

    # FCM (Firebase Cloud Messaging)
    FCM_PROJECT_ID: str | None = None
    FCM_CREDENTIALS_PATH: str | None = None  # Path to service account JSON
    FCM_DRY_RUN: bool = False  # For testing without sending real notifications

    # APNs (Apple Push Notification service)
    APNS_KEY_ID: str | None = None  # Key ID from Apple Developer Portal
    APNS_TEAM_ID: str | None = None  # Apple Team ID
    APNS_BUNDLE_ID: str | None = None  # iOS app bundle identifier
    APNS_KEY_PATH: str | None = None  # Path to .p8 key file
    APNS_USE_SANDBOX: bool = True  # Use sandbox for development, False for production

    # RevenueCat (In-App Purchases)
    REVENUECAT_API_KEY: str | None = None  # RevenueCat public API key
    REVENUECAT_WEBHOOK_SECRET: str | None = None  # Webhook signature verification secret

    # Worker settings
    WORKER_ENABLED: bool = True
    WORKER_CREATE_EVENTS_HOUR: int = 23  # UTC hour for daily event creation
    WORKER_CREATE_EVENTS_MINUTE: int = 0
    WORKER_OVERDUE_CHECK_MINUTES: int = 15  # Interval for checking overdue events
    WORKER_OVERDUE_THRESHOLD_HOURS: int = 2  # Hours after scheduled time to mark missed
    WORKER_CLEANUP_RETENTION_DAYS: int = 90  # Days to keep old events

    # Notification job settings
    NOTIFICATION_WEEKLY_SUMMARY_HOUR: int = 10  # UTC hour for weekly summary (Sunday)
    NOTIFICATION_WEEKLY_SUMMARY_MINUTE: int = 0
    NOTIFICATION_RE_ENGAGEMENT_HOUR: int = 12  # UTC hour for daily re-engagement check
    NOTIFICATION_RE_ENGAGEMENT_MINUTE: int = 0
    NOTIFICATION_INACTIVITY_DAYS: int = 3  # Days of inactivity before re-engagement push

    # Subscription job settings
    SUBSCRIPTION_CHECK_INTERVAL_MINUTES: int = 15  # Interval for checking expired subscriptions
    SUBSCRIPTION_BATCH_SIZE: int = 100  # Batch size for processing expired subscriptions

    # Analytics
    ANALYTICS_FORWARD_URL: str | None = None  # PostHog/Amplitude endpoint URL
    ANALYTICS_IP_SALT: str = "fishfeed-analytics-salt"  # Salt for IP hashing
    ANALYTICS_FORWARD_TIMEOUT_SECONDS: int = 10  # Timeout for external analytics forwarding
    ANALYTICS_FORWARD_MAX_RETRIES: int = 3  # Max retries for external forwarding

    # Mobile Releases
    RELEASES_DIR: str = "/app/mobile/releases"

    # Analytics Cleanup (GDPR compliance)
    ANALYTICS_ANONYMIZE_AFTER_DAYS: int = 30  # Days before anonymizing events
    ANALYTICS_RETENTION_DAYS: int = 90  # Days before deleting events
    ANALYTICS_CLEANUP_BATCH_SIZE: int = 1000  # Batch size for cleanup operations
    ANALYTICS_CLEANUP_HOUR: int = 3  # UTC hour for daily cleanup job
    ANALYTICS_CLEANUP_MINUTE: int = 0  # Minute for daily cleanup job


@lru_cache
def get_settings() -> Settings:
    return Settings()
