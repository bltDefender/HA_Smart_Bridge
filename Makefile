.PHONY: up down logs test lint validate schema ui

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f bridge

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

validate:
	uv run bridge validate --all

schema:
	uv run bridge schema -o docs/rule.schema.json

ui:
	cd ui && npm ci && npm run check && npm test && npm run build
