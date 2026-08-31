.PHONY: dev dev-down dev-prod test test-unit test-integration test-cov lint lint-fix migrate health clean frontend-build frontend-test m5-doctor m5-conformance m5-demo m5-report

# Canonical M5 behavior lives only in scripts/finrpa_release.py.
m5-doctor:
	python scripts/finrpa_release.py doctor

m5-conformance:
	python scripts/finrpa_release.py conformance

m5-demo:
	python scripts/finrpa_release.py demo

m5-report:
	python scripts/finrpa_release.py report

# ============================================================
# Development
# ============================================================

dev:
	docker compose up -d
	@echo ""
	@echo "Services:"
	@echo "  Skyvern API:    http://localhost:8000"
	@echo "  Skyvern UI:     http://localhost:8080"
	@echo "  PostgreSQL:     localhost:5432"
	@echo "  Redis:          localhost:6379"

dev-down:
	docker compose down

dev-prod:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

health:
	@echo "=== Service Health ==="
	@curl -sf http://localhost:8000/api/v1/health > /dev/null 2>&1 && echo "  [OK] Skyvern API" || echo "  [FAIL] Skyvern API"
	@docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG && echo "  [OK] Redis" || echo "  [FAIL] Redis"
	@docker compose exec -T postgres pg_isready -U skyvern 2>/dev/null | grep -q "accepting" && echo "  [OK] PostgreSQL" || echo "  [FAIL] PostgreSQL"

# ============================================================
# Testing
# ============================================================

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-cov:
	pytest tests/ -v --cov=enterprise --cov-report=term-missing

# ============================================================
# Code Quality
# ============================================================

lint:
	ruff check enterprise/ tests/
	mypy enterprise/ --ignore-missing-imports

lint-fix:
	ruff check enterprise/ tests/ --fix

# ============================================================
# Frontend
# ============================================================

frontend-build:
	cd skyvern-frontend && npm run build

frontend-test:
	cd skyvern-frontend && npx vitest run

# ============================================================
# Database
# ============================================================

migrate:
	alembic upgrade head

# ============================================================
# Cleanup
# ============================================================

clean:
	docker compose down -v
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
