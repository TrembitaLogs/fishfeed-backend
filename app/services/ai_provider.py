"""AI provider service for fish species classification.

This module provides an abstraction layer for different AI services
that can classify fish species from images.
"""

import asyncio
import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class AIProviderType(str, Enum):
    """Supported AI provider types."""

    GOOGLE_VISION = "google_vision"
    REPLICATE = "replicate"


class AIProviderError(Exception):
    """Base exception for AI provider errors."""

    def __init__(self, message: str, provider: str, retriable: bool = False):
        self.message = message
        self.provider = provider
        self.retriable = retriable
        super().__init__(message)


class AIProviderTimeoutError(AIProviderError):
    """Raised when AI provider request times out."""

    def __init__(self, provider: str):
        super().__init__(
            f"Request to {provider} timed out",
            provider=provider,
            retriable=True,
        )


class AIProviderUnavailableError(AIProviderError):
    """Raised when AI provider is unavailable."""

    def __init__(self, provider: str, detail: str = ""):
        message = f"AI provider {provider} is unavailable"
        if detail:
            message += f": {detail}"
        super().__init__(message, provider=provider, retriable=True)


class AIProviderConfigError(AIProviderError):
    """Raised when AI provider is misconfigured."""

    def __init__(self, provider: str, detail: str):
        super().__init__(
            f"AI provider {provider} configuration error: {detail}",
            provider=provider,
            retriable=False,
        )


@dataclass
class Prediction:
    """Single prediction result from AI classification."""

    label: str
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")


@dataclass
class ClassificationResult:
    """Result from AI fish classification."""

    predictions: list[Prediction] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)

    def filter_by_threshold(self, threshold: float) -> ClassificationResult:
        """Return new result with predictions filtered by confidence threshold."""
        filtered = [p for p in self.predictions if p.confidence >= threshold]
        return ClassificationResult(
            predictions=filtered,
            raw_response=self.raw_response,
        )

    @property
    def top_prediction(self) -> Prediction | None:
        """Return the prediction with highest confidence."""
        if not self.predictions:
            return None
        return max(self.predictions, key=lambda p: p.confidence)


class AIProvider(ABC):
    """Abstract base class for AI fish classification providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return provider name for logging and identification."""
        ...

    @abstractmethod
    async def classify_fish(self, image_bytes: bytes) -> ClassificationResult:
        """Classify fish species from image bytes.

        Args:
            image_bytes: Preprocessed image as bytes (JPEG format, 512x512).

        Returns:
            ClassificationResult with predictions sorted by confidence.

        Raises:
            AIProviderError: If classification fails.
        """
        ...

    async def health_check(self) -> bool:
        """Check if provider is available and properly configured.

        Returns:
            True if provider is healthy, False otherwise.
        """
        return True


class GoogleVisionProvider(AIProvider):
    """Google Cloud Vision API provider for fish classification.

    Uses label detection to identify fish species in images.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._settings = get_settings()

    @property
    def name(self) -> str:
        return "google_vision"

    def _get_client(self) -> Any:
        """Get or create Google Vision client (lazy initialization)."""
        if self._client is None:
            try:
                from google.cloud import vision
            except ImportError as e:
                raise AIProviderConfigError(
                    self.name,
                    "google-cloud-vision package not installed",
                ) from e

            try:
                self._client = vision.ImageAnnotatorClient()
            except Exception as e:
                raise AIProviderConfigError(
                    self.name,
                    f"Failed to initialize client: {e}",
                ) from e

        return self._client

    async def classify_fish(self, image_bytes: bytes) -> ClassificationResult:
        """Classify fish using Google Cloud Vision label detection."""
        settings = self._settings
        timeout = settings.AI_REQUEST_TIMEOUT_SECONDS

        try:
            from google.cloud import vision
        except ImportError as e:
            raise AIProviderConfigError(
                self.name,
                "google-cloud-vision package not installed",
            ) from e

        try:
            client = self._get_client()

            image = vision.Image(content=image_bytes)
            request = vision.AnnotateImageRequest(
                image=image,
                features=[
                    vision.Feature(
                        type_=vision.Feature.Type.LABEL_DETECTION,
                        max_results=10,
                    )
                ],
            )

            loop = asyncio.get_event_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: client.annotate_image(request),
                ),
                timeout=timeout,
            )

            predictions = []
            for label in response.label_annotations:
                predictions.append(
                    Prediction(
                        label=label.description,
                        confidence=label.score,
                    )
                )

            predictions.sort(key=lambda p: p.confidence, reverse=True)

            raw_response = {
                "labels": [
                    {"description": label.description, "score": label.score}
                    for label in response.label_annotations
                ]
            }

            return ClassificationResult(
                predictions=predictions,
                raw_response=raw_response,
            )

        except TimeoutError:
            raise AIProviderTimeoutError(self.name) from None
        except AIProviderError:
            raise
        except Exception as e:
            logger.error("Google Vision API error", error=str(e))
            raise AIProviderUnavailableError(self.name, str(e)) from e

    async def health_check(self) -> bool:
        """Check if Google Vision is properly configured."""
        try:
            self._get_client()
            return True
        except AIProviderError:
            return False


class ReplicateProvider(AIProvider):
    """Replicate API provider for fish classification.

    Uses a fish classification model hosted on Replicate.
    """

    FISH_MODEL = "andreasjansson/fish-identifier:1dc4b2db21bcf1c98c47a2d9c12b0d6a8b96d2e3"
    FALLBACK_MODEL = "salesforce/blip:2e1dddc8621f72155f24cf2e0adbde548458d3cab9f00c0139eea840d0ac4746"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "replicate"

    def _get_api_token(self) -> str:
        """Get Replicate API token from settings."""
        token = self._settings.REPLICATE_API_TOKEN
        if not token:
            raise AIProviderConfigError(
                self.name,
                "REPLICATE_API_TOKEN not configured",
            )
        return token

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.replicate.com/v1",
                headers={
                    "Authorization": f"Token {self._get_api_token()}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self._settings.AI_REQUEST_TIMEOUT_SECONDS,
                    write=10.0,
                    pool=10.0,
                ),
            )
        return self._client

    async def classify_fish(self, image_bytes: bytes) -> ClassificationResult:
        """Classify fish using Replicate API."""
        settings = self._settings
        timeout = settings.AI_REQUEST_TIMEOUT_SECONDS

        try:
            client = await self._get_client()

            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
            data_uri = f"data:image/jpeg;base64,{image_base64}"

            create_response = await client.post(
                "/predictions",
                json={
                    "version": self.FALLBACK_MODEL.split(":")[1],
                    "input": {
                        "image": data_uri,
                        "task": "image_captioning",
                    },
                },
            )

            if create_response.status_code == 401:
                raise AIProviderConfigError(
                    self.name,
                    "Invalid API token",
                )
            elif create_response.status_code == 422:
                raise AIProviderError(
                    "Invalid input parameters",
                    provider=self.name,
                    retriable=False,
                )
            elif create_response.status_code >= 500:
                raise AIProviderUnavailableError(
                    self.name,
                    f"Server error: {create_response.status_code}",
                )

            create_response.raise_for_status()
            prediction_data = create_response.json()
            prediction_id = prediction_data["id"]

            result = await self._poll_prediction(
                client,
                prediction_id,
                timeout,
            )

            return self._parse_replicate_response(result)

        except httpx.TimeoutException:
            raise AIProviderTimeoutError(self.name) from None
        except AIProviderError:
            raise
        except Exception as e:
            logger.error("Replicate API error", error=str(e))
            raise AIProviderUnavailableError(self.name, str(e)) from e

    async def _poll_prediction(
        self,
        client: httpx.AsyncClient,
        prediction_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Poll for prediction completion."""
        poll_interval = 0.5
        elapsed = 0.0

        while elapsed < timeout:
            response = await client.get(f"/predictions/{prediction_id}")
            response.raise_for_status()
            data = response.json()

            status = data.get("status")
            if status == "succeeded":
                return dict(data)
            elif status in ("failed", "canceled"):
                error = data.get("error", "Unknown error")
                raise AIProviderError(
                    f"Prediction failed: {error}",
                    provider=self.name,
                    retriable=False,
                )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise AIProviderTimeoutError(self.name)

    def _parse_replicate_response(
        self,
        response: dict[str, Any],
    ) -> ClassificationResult:
        """Parse Replicate API response into ClassificationResult."""
        output = response.get("output", "")
        predictions = []

        if isinstance(output, str) and output:
            predictions.append(
                Prediction(
                    label=output,
                    confidence=0.7,
                )
            )
        elif isinstance(output, list):
            for i, item in enumerate(output[:5]):
                if isinstance(item, dict):
                    label = str(item.get("label", item.get("name", str(item))))
                    confidence = float(item.get("confidence", item.get("score", 0.5)) or 0.5)
                else:
                    label = str(item)
                    confidence = max(0.9 - (i * 0.1), 0.5)
                predictions.append(
                    Prediction(label=label, confidence=confidence)
                )

        return ClassificationResult(
            predictions=predictions,
            raw_response=response,
        )

    async def health_check(self) -> bool:
        """Check if Replicate API is accessible."""
        try:
            self._get_api_token()
            client = await self._get_client()
            response = await client.get("/models")
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


_provider_instance: AIProvider | None = None
_fallback_provider_instance: AIProvider | None = None


def _create_provider(provider_type: AIProviderType) -> AIProvider:
    """Create AI provider instance by type."""
    if provider_type == AIProviderType.GOOGLE_VISION:
        return GoogleVisionProvider()
    elif provider_type == AIProviderType.REPLICATE:
        return ReplicateProvider()
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")


def get_ai_provider() -> AIProvider:
    """Get configured AI provider instance (singleton).

    Returns the primary provider as configured in settings.
    Use get_ai_provider_with_fallback() for automatic fallback support.

    Returns:
        Configured AIProvider instance.

    Raises:
        AIProviderConfigError: If provider configuration is invalid.
    """
    global _provider_instance

    if _provider_instance is not None:
        return _provider_instance

    settings = get_settings()
    provider_type_str = settings.AI_PROVIDER.lower()

    try:
        provider_type = AIProviderType(provider_type_str)
    except ValueError:
        raise AIProviderConfigError(
            "unknown",
            f"Invalid AI_PROVIDER value: {provider_type_str}. "
            f"Supported: {[t.value for t in AIProviderType]}",
        ) from None

    _provider_instance = _create_provider(provider_type)
    return _provider_instance


def _get_fallback_provider() -> AIProvider | None:
    """Get fallback provider if different from primary."""
    global _fallback_provider_instance

    if _fallback_provider_instance is not None:
        return _fallback_provider_instance

    settings = get_settings()
    primary_type = settings.AI_PROVIDER.lower()

    if primary_type == AIProviderType.GOOGLE_VISION.value:
        if settings.REPLICATE_API_TOKEN:
            _fallback_provider_instance = ReplicateProvider()
    elif primary_type == AIProviderType.REPLICATE.value:
        try:
            from google.cloud import vision  # noqa: F401

            _fallback_provider_instance = GoogleVisionProvider()
        except ImportError:
            pass

    return _fallback_provider_instance


async def classify_with_fallback(
    image_bytes: bytes,
    min_confidence: float | None = None,
) -> ClassificationResult:
    """Classify fish with automatic fallback to secondary provider.

    If the primary provider fails with a retriable error, attempts
    classification with the fallback provider.

    Args:
        image_bytes: Preprocessed image as bytes.
        min_confidence: Optional confidence threshold for filtering results.
            Defaults to AI_MIN_CONFIDENCE_THRESHOLD from settings.

    Returns:
        ClassificationResult with predictions.

    Raises:
        AIProviderError: If both providers fail.
    """
    settings = get_settings()
    if min_confidence is None:
        min_confidence = settings.AI_MIN_CONFIDENCE_THRESHOLD

    primary = get_ai_provider()
    fallback = _get_fallback_provider()

    try:
        result = await primary.classify_fish(image_bytes)
        logger.info("Classification successful", provider=primary.name)
        return result.filter_by_threshold(min_confidence)

    except AIProviderError as e:
        logger.warning("Primary provider failed", provider=primary.name, error=str(e))

        if not e.retriable or fallback is None:
            raise

        logger.info("Attempting fallback", provider=fallback.name)
        try:
            result = await fallback.classify_fish(image_bytes)
            logger.info("Fallback classification successful", provider=fallback.name)
            return result.filter_by_threshold(min_confidence)
        except AIProviderError as fallback_error:
            logger.error("Fallback provider also failed", provider=fallback.name, error=str(fallback_error))
            raise e from fallback_error


def reset_providers() -> None:
    """Reset provider instances (useful for testing)."""
    global _provider_instance, _fallback_provider_instance
    _provider_instance = None
    _fallback_provider_instance = None
