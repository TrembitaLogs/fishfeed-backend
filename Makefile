.PHONY: up down logs test lint migrate makemigrations shell seed-species seed-species-dry-run seed-species-clear

up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f

test:
	uv run pytest

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
