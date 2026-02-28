.PHONY: up down logs test test-images lint migrate makemigrations shell seed-species seed-species-dry-run seed-species-clear

up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f

test:
	uv run pytest

test-images:
	uv run pytest tests/api/test_images.py tests/services/test_image_service.py tests/schemas/test_image.py tests/jobs/test_image_cleanup.py tests/e2e/test_image_sync.py -v

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check . --fix

migrate:
	uv run alembic upgrade head

makemigrations:
	uv run alembic revision --autogenerate -m "$(msg)"

shell:
	docker-compose exec api bash

seed-species:
	uv run python scripts/seed_species.py

seed-species-dry-run:
	uv run python scripts/seed_species.py --dry-run

seed-species-clear:
	uv run python scripts/seed_species.py --clear-existing
