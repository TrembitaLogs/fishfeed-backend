"""Tests for AI provider service."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.ai_provider import (
    AIProvider,
    AIProviderConfigError,
    AIProviderError,
    AIProviderTimeoutError,
    AIProviderType,
    AIProviderUnavailableError,
    ClassificationResult,
    GoogleVisionProvider,
    Prediction,
    ReplicateProvider,
    classify_with_fallback,
    get_ai_provider,
    reset_providers,
)


class TestPrediction:
    """Tests for Prediction dataclass."""

    def test_create_valid_prediction(self):
        """Test creating valid prediction."""
        pred = Prediction(label="goldfish", confidence=0.95)
        assert pred.label == "goldfish"
        assert pred.confidence == 0.95

    def test_confidence_at_boundaries(self):
        """Test confidence at valid boundaries."""
        pred_min = Prediction(label="fish", confidence=0.0)
        pred_max = Prediction(label="fish", confidence=1.0)
        assert pred_min.confidence == 0.0
        assert pred_max.confidence == 1.0

    def test_invalid_confidence_below_zero_raises_error(self):
        """Test that confidence below 0 raises error."""
        with pytest.raises(ValueError) as exc_info:
            Prediction(label="fish", confidence=-0.1)
        assert "confidence must be between 0.0 and 1.0" in str(exc_info.value)

    def test_invalid_confidence_above_one_raises_error(self):
        """Test that confidence above 1 raises error."""
        with pytest.raises(ValueError) as exc_info:
            Prediction(label="fish", confidence=1.1)
        assert "confidence must be between 0.0 and 1.0" in str(exc_info.value)


class TestClassificationResult:
    """Tests for ClassificationResult dataclass."""

    def test_empty_result(self):
        """Test creating empty classification result."""
        result = ClassificationResult()
        assert result.predictions == []
        assert result.raw_response == {}
        assert result.top_prediction is None

    def test_top_prediction_returns_highest_confidence(self):
        """Test top_prediction returns prediction with highest confidence."""
        predictions = [
            Prediction(label="guppy", confidence=0.7),
            Prediction(label="goldfish", confidence=0.95),
            Prediction(label="betta", confidence=0.85),
        ]
        result = ClassificationResult(predictions=predictions)

        top = result.top_prediction
        assert top is not None
        assert top.label == "goldfish"
        assert top.confidence == 0.95

    def test_filter_by_threshold_filters_low_confidence(self):
        """Test filter_by_threshold removes low confidence predictions."""
        predictions = [
            Prediction(label="goldfish", confidence=0.9),
            Prediction(label="guppy", confidence=0.6),
            Prediction(label="unknown", confidence=0.3),
        ]
        result = ClassificationResult(predictions=predictions)

        filtered = result.filter_by_threshold(0.5)
        assert len(filtered.predictions) == 2
        assert all(p.confidence >= 0.5 for p in filtered.predictions)

    def test_filter_preserves_raw_response(self):
        """Test filter_by_threshold preserves raw_response."""
        raw = {"original": "data"}
        result = ClassificationResult(
            predictions=[Prediction(label="fish", confidence=0.8)],
            raw_response=raw,
        )
        filtered = result.filter_by_threshold(0.5)
        assert filtered.raw_response == raw


class TestGoogleVisionProvider:
    """Tests for GoogleVisionProvider."""

    def test_provider_name(self):
        """Test provider name property."""
        provider = GoogleVisionProvider()
        assert provider.name == "google_vision"

    @pytest.mark.asyncio
    async def test_classify_fish_success(self):
        """Test successful fish classification with Google Vision."""
        provider = GoogleVisionProvider()

        mock_label = MagicMock()
        mock_label.description = "goldfish"
        mock_label.score = 0.95

        mock_label2 = MagicMock()
        mock_label2.description = "fish"
        mock_label2.score = 0.85

        mock_response = MagicMock()
        mock_response.label_annotations = [mock_label, mock_label2]

        mock_client = MagicMock()
        mock_client.annotate_image = MagicMock(return_value=mock_response)

        # Mock the google.cloud.vision module
        mock_vision = MagicMock()
        mock_vision.Image = MagicMock()
        mock_vision.AnnotateImageRequest = MagicMock()
        mock_vision.Feature = MagicMock()
        mock_vision.Feature.Type.LABEL_DETECTION = "LABEL_DETECTION"

        with patch.dict("sys.modules", {"google.cloud": MagicMock(), "google.cloud.vision": mock_vision}):
            with patch.object(provider, "_get_client", return_value=mock_client):
                with patch("app.services.ai_provider.asyncio.get_event_loop") as mock_loop:
                    mock_loop.return_value.run_in_executor = AsyncMock(
                        return_value=mock_response
                    )
                    result = await provider.classify_fish(b"fake_image_bytes")

        assert len(result.predictions) == 2
        assert result.predictions[0].label == "goldfish"
        assert result.predictions[0].confidence == 0.95

    @pytest.mark.asyncio
    async def test_classify_fish_timeout(self):
        """Test timeout handling in Google Vision."""
        provider = GoogleVisionProvider()

        mock_client = MagicMock()

        # Mock the google.cloud.vision module
        mock_vision = MagicMock()
        mock_vision.Image = MagicMock()
        mock_vision.AnnotateImageRequest = MagicMock()
        mock_vision.Feature = MagicMock()
        mock_vision.Feature.Type.LABEL_DETECTION = "LABEL_DETECTION"

        with patch.dict("sys.modules", {"google.cloud": MagicMock(), "google.cloud.vision": mock_vision}):
            with patch.object(provider, "_get_client", return_value=mock_client):
                with patch("app.services.ai_provider.asyncio.wait_for") as mock_wait:
                    mock_wait.side_effect = TimeoutError()
                    with pytest.raises(AIProviderTimeoutError) as exc_info:
                        await provider.classify_fish(b"fake_image_bytes")

        assert exc_info.value.provider == "google_vision"
        assert exc_info.value.retriable is True

    def test_get_client_import_error(self):
        """Test import error when google-cloud-vision not installed."""
        provider = GoogleVisionProvider()

        with patch.dict("sys.modules", {"google.cloud": None, "google": None}):
            with patch(
                "builtins.__import__",
                side_effect=ImportError("No module named 'google.cloud'"),
            ):
                with pytest.raises(AIProviderConfigError) as exc_info:
                    provider._get_client()

        assert "google-cloud-vision" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        """Test health check when provider is configured."""
        provider = GoogleVisionProvider()

        with patch.object(provider, "_get_client", return_value=MagicMock()):
            result = await provider.health_check()

        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        """Test health check when provider is not configured."""
        provider = GoogleVisionProvider()

        with patch.object(
            provider,
            "_get_client",
            side_effect=AIProviderConfigError("google_vision", "not configured"),
        ):
            result = await provider.health_check()

        assert result is False


class TestReplicateProvider:
    """Tests for ReplicateProvider."""

    def test_provider_name(self):
        """Test provider name property."""
        provider = ReplicateProvider()
        assert provider.name == "replicate"

    def test_get_api_token_missing_raises_error(self):
        """Test missing API token raises config error."""
        provider = ReplicateProvider()

        with patch.object(provider._settings, "REPLICATE_API_TOKEN", None):
            with pytest.raises(AIProviderConfigError) as exc_info:
                provider._get_api_token()

        assert "REPLICATE_API_TOKEN" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_classify_fish_success(self):
        """Test successful fish classification with Replicate."""
        provider = ReplicateProvider()

        create_response = MagicMock()
        create_response.status_code = 201
        create_response.json.return_value = {"id": "prediction-123"}
        create_response.raise_for_status = MagicMock()

        get_response = MagicMock()
        get_response.status_code = 200
        get_response.json.return_value = {
            "id": "prediction-123",
            "status": "succeeded",
            "output": "A beautiful goldfish swimming",
        }
        get_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_response)
        mock_client.get = AsyncMock(return_value=get_response)
        mock_client.is_closed = False

        with patch.object(provider, "_get_api_token", return_value="test-token"):
            with patch.object(provider, "_get_client", return_value=mock_client):
                result = await provider.classify_fish(b"fake_image_bytes")

        assert len(result.predictions) == 1
        assert "goldfish" in result.predictions[0].label

    @pytest.mark.asyncio
    async def test_classify_fish_invalid_token(self):
        """Test invalid API token returns config error."""
        provider = ReplicateProvider()

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False

        with patch.object(provider, "_get_api_token", return_value="invalid-token"):
            with patch.object(provider, "_get_client", return_value=mock_client):
                with pytest.raises(AIProviderConfigError) as exc_info:
                    await provider.classify_fish(b"fake_image_bytes")

        assert "Invalid API token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_classify_fish_server_error(self):
        """Test server error returns unavailable error."""
        provider = ReplicateProvider()

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False

        with patch.object(provider, "_get_api_token", return_value="test-token"):
            with patch.object(provider, "_get_client", return_value=mock_client):
                with pytest.raises(AIProviderUnavailableError) as exc_info:
                    await provider.classify_fish(b"fake_image_bytes")

        assert exc_info.value.retriable is True

    @pytest.mark.asyncio
    async def test_classify_fish_timeout(self):
        """Test timeout during classification."""
        provider = ReplicateProvider()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.is_closed = False

        with patch.object(provider, "_get_api_token", return_value="test-token"):
            with patch.object(provider, "_get_client", return_value=mock_client):
                with pytest.raises(AIProviderTimeoutError) as exc_info:
                    await provider.classify_fish(b"fake_image_bytes")

        assert exc_info.value.provider == "replicate"

    @pytest.mark.asyncio
    async def test_poll_prediction_failed_status(self):
        """Test polling handles failed prediction status."""
        provider = ReplicateProvider()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "failed",
            "error": "Model error occurred",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(AIProviderError) as exc_info:
            await provider._poll_prediction(mock_client, "pred-123", timeout=10)

        assert "Model error occurred" in str(exc_info.value)
        assert exc_info.value.retriable is False

    def test_parse_replicate_response_string_output(self):
        """Test parsing string output from Replicate."""
        provider = ReplicateProvider()

        response = {"output": "A goldfish in a tank"}
        result = provider._parse_replicate_response(response)

        assert len(result.predictions) == 1
        assert result.predictions[0].label == "A goldfish in a tank"
        assert result.predictions[0].confidence == 0.7

    def test_parse_replicate_response_list_output(self):
        """Test parsing list output from Replicate."""
        provider = ReplicateProvider()

        response = {
            "output": [
                {"label": "goldfish", "confidence": 0.9},
                {"label": "guppy", "confidence": 0.7},
            ]
        }
        result = provider._parse_replicate_response(response)

        assert len(result.predictions) == 2
        assert result.predictions[0].label == "goldfish"
        assert result.predictions[0].confidence == 0.9

    def test_parse_replicate_response_empty_output(self):
        """Test parsing empty output from Replicate."""
        provider = ReplicateProvider()

        response = {"output": ""}
        result = provider._parse_replicate_response(response)

        assert len(result.predictions) == 0

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        """Test health check when API is accessible."""
        provider = ReplicateProvider()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False

        with patch.object(provider, "_get_api_token", return_value="test-token"):
            with patch.object(provider, "_get_client", return_value=mock_client):
                result = await provider.health_check()

        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        """Test health check when API is not accessible."""
        provider = ReplicateProvider()

        with patch.object(
            provider,
            "_get_api_token",
            side_effect=AIProviderConfigError("replicate", "no token"),
        ):
            result = await provider.health_check()

        assert result is False


class TestGetAIProvider:
    """Tests for get_ai_provider factory function."""

    def setup_method(self):
        """Reset providers before each test."""
        reset_providers()

    def teardown_method(self):
        """Reset providers after each test."""
        reset_providers()

    def test_get_google_vision_provider(self):
        """Test getting Google Vision provider."""
        with patch("app.services.ai_provider.get_settings") as mock_settings:
            mock_settings.return_value.AI_PROVIDER = "google_vision"
            provider = get_ai_provider()

        assert isinstance(provider, GoogleVisionProvider)

    def test_get_replicate_provider(self):
        """Test getting Replicate provider."""
        reset_providers()
        with patch("app.services.ai_provider.get_settings") as mock_settings:
            mock_settings.return_value.AI_PROVIDER = "replicate"
            provider = get_ai_provider()

        assert isinstance(provider, ReplicateProvider)

    def test_invalid_provider_type_raises_error(self):
        """Test invalid provider type raises config error."""
        reset_providers()
        with patch("app.services.ai_provider.get_settings") as mock_settings:
            mock_settings.return_value.AI_PROVIDER = "invalid_provider"
            with pytest.raises(AIProviderConfigError) as exc_info:
                get_ai_provider()

        assert "Invalid AI_PROVIDER" in str(exc_info.value)

    def test_provider_is_singleton(self):
        """Test that provider is a singleton."""
        reset_providers()
        with patch("app.services.ai_provider.get_settings") as mock_settings:
            mock_settings.return_value.AI_PROVIDER = "google_vision"
            provider1 = get_ai_provider()
            provider2 = get_ai_provider()

        assert provider1 is provider2


class TestClassifyWithFallback:
    """Tests for classify_with_fallback function."""

    def setup_method(self):
        """Reset providers before each test."""
        reset_providers()

    def teardown_method(self):
        """Reset providers after each test."""
        reset_providers()

    @pytest.mark.asyncio
    async def test_primary_success_no_fallback_needed(self):
        """Test successful classification without fallback."""
        mock_result = ClassificationResult(
            predictions=[Prediction(label="goldfish", confidence=0.9)]
        )

        mock_provider = AsyncMock(spec=AIProvider)
        mock_provider.name = "test_primary"
        mock_provider.classify_fish = AsyncMock(return_value=mock_result)

        with patch("app.services.ai_provider.get_ai_provider", return_value=mock_provider):
            with patch("app.services.ai_provider._get_fallback_provider", return_value=None):
                with patch("app.services.ai_provider.get_settings") as mock_settings:
                    mock_settings.return_value.AI_MIN_CONFIDENCE_THRESHOLD = 0.5
                    result = await classify_with_fallback(b"image_bytes")

        assert len(result.predictions) == 1
        assert result.predictions[0].label == "goldfish"

    @pytest.mark.asyncio
    async def test_primary_fails_fallback_succeeds(self):
        """Test fallback when primary provider fails."""
        mock_result = ClassificationResult(
            predictions=[Prediction(label="betta", confidence=0.85)]
        )

        mock_primary = AsyncMock(spec=AIProvider)
        mock_primary.name = "test_primary"
        mock_primary.classify_fish = AsyncMock(
            side_effect=AIProviderTimeoutError("test_primary")
        )

        mock_fallback = AsyncMock(spec=AIProvider)
        mock_fallback.name = "test_fallback"
        mock_fallback.classify_fish = AsyncMock(return_value=mock_result)

        with patch("app.services.ai_provider.get_ai_provider", return_value=mock_primary):
            with patch("app.services.ai_provider._get_fallback_provider", return_value=mock_fallback):
                with patch("app.services.ai_provider.get_settings") as mock_settings:
                    mock_settings.return_value.AI_MIN_CONFIDENCE_THRESHOLD = 0.5
                    result = await classify_with_fallback(b"image_bytes")

        assert len(result.predictions) == 1
        assert result.predictions[0].label == "betta"

    @pytest.mark.asyncio
    async def test_both_providers_fail(self):
        """Test error when both providers fail."""
        mock_primary = AsyncMock(spec=AIProvider)
        mock_primary.name = "test_primary"
        mock_primary.classify_fish = AsyncMock(
            side_effect=AIProviderTimeoutError("test_primary")
        )

        mock_fallback = AsyncMock(spec=AIProvider)
        mock_fallback.name = "test_fallback"
        mock_fallback.classify_fish = AsyncMock(
            side_effect=AIProviderUnavailableError("test_fallback", "down")
        )

        with patch("app.services.ai_provider.get_ai_provider", return_value=mock_primary):
            with patch("app.services.ai_provider._get_fallback_provider", return_value=mock_fallback):
                with patch("app.services.ai_provider.get_settings") as mock_settings:
                    mock_settings.return_value.AI_MIN_CONFIDENCE_THRESHOLD = 0.5
                    with pytest.raises(AIProviderTimeoutError):
                        await classify_with_fallback(b"image_bytes")

    @pytest.mark.asyncio
    async def test_non_retriable_error_no_fallback(self):
        """Test non-retriable error does not trigger fallback."""
        mock_primary = AsyncMock(spec=AIProvider)
        mock_primary.name = "test_primary"
        mock_primary.classify_fish = AsyncMock(
            side_effect=AIProviderConfigError("test_primary", "bad config")
        )

        mock_fallback = AsyncMock(spec=AIProvider)
        mock_fallback.name = "test_fallback"
        mock_fallback.classify_fish = AsyncMock()

        with patch("app.services.ai_provider.get_ai_provider", return_value=mock_primary):
            with patch("app.services.ai_provider._get_fallback_provider", return_value=mock_fallback):
                with patch("app.services.ai_provider.get_settings") as mock_settings:
                    mock_settings.return_value.AI_MIN_CONFIDENCE_THRESHOLD = 0.5
                    with pytest.raises(AIProviderConfigError):
                        await classify_with_fallback(b"image_bytes")

        # Fallback should not be called for non-retriable errors
        mock_fallback.classify_fish.assert_not_called()

    @pytest.mark.asyncio
    async def test_confidence_threshold_applied(self):
        """Test confidence threshold is applied to results."""
        mock_result = ClassificationResult(
            predictions=[
                Prediction(label="goldfish", confidence=0.9),
                Prediction(label="guppy", confidence=0.4),
            ]
        )

        mock_provider = AsyncMock(spec=AIProvider)
        mock_provider.name = "test_primary"
        mock_provider.classify_fish = AsyncMock(return_value=mock_result)

        with patch("app.services.ai_provider.get_ai_provider", return_value=mock_provider):
            with patch("app.services.ai_provider._get_fallback_provider", return_value=None):
                with patch("app.services.ai_provider.get_settings") as mock_settings:
                    mock_settings.return_value.AI_MIN_CONFIDENCE_THRESHOLD = 0.5
                    result = await classify_with_fallback(b"image_bytes")

        # Only predictions above threshold should remain
        assert len(result.predictions) == 1
        assert result.predictions[0].label == "goldfish"

    @pytest.mark.asyncio
    async def test_custom_confidence_threshold(self):
        """Test custom confidence threshold parameter."""
        mock_result = ClassificationResult(
            predictions=[
                Prediction(label="goldfish", confidence=0.9),
                Prediction(label="guppy", confidence=0.7),
                Prediction(label="betta", confidence=0.5),
            ]
        )

        mock_provider = AsyncMock(spec=AIProvider)
        mock_provider.name = "test_primary"
        mock_provider.classify_fish = AsyncMock(return_value=mock_result)

        with patch("app.services.ai_provider.get_ai_provider", return_value=mock_provider):
            with patch("app.services.ai_provider._get_fallback_provider", return_value=None):
                result = await classify_with_fallback(
                    b"image_bytes",
                    min_confidence=0.8,
                )

        assert len(result.predictions) == 1
        assert result.predictions[0].label == "goldfish"


class TestAIProviderType:
    """Tests for AIProviderType enum."""

    def test_google_vision_value(self):
        """Test Google Vision enum value."""
        assert AIProviderType.GOOGLE_VISION.value == "google_vision"

    def test_replicate_value(self):
        """Test Replicate enum value."""
        assert AIProviderType.REPLICATE.value == "replicate"

    def test_enum_from_string(self):
        """Test creating enum from string."""
        provider_type = AIProviderType("google_vision")
        assert provider_type == AIProviderType.GOOGLE_VISION


class TestAIProviderErrors:
    """Tests for AI provider error classes."""

    def test_base_error(self):
        """Test base AIProviderError."""
        error = AIProviderError("test message", provider="test", retriable=True)
        assert error.message == "test message"
        assert error.provider == "test"
        assert error.retriable is True
        assert str(error) == "test message"

    def test_timeout_error(self):
        """Test AIProviderTimeoutError."""
        error = AIProviderTimeoutError("google_vision")
        assert "google_vision" in error.message
        assert "timed out" in error.message
        assert error.retriable is True

    def test_unavailable_error(self):
        """Test AIProviderUnavailableError."""
        error = AIProviderUnavailableError("replicate", "server down")
        assert "replicate" in error.message
        assert "unavailable" in error.message
        assert "server down" in error.message
        assert error.retriable is True

    def test_config_error(self):
        """Test AIProviderConfigError."""
        error = AIProviderConfigError("google_vision", "missing credentials")
        assert "google_vision" in error.message
        assert "missing credentials" in error.message
        assert error.retriable is False
