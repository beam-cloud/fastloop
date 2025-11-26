VERSION=$(shell grep '^version =' pyproject.toml | head -1 | cut -d'"' -f2)

.PHONY: test test-verbose install-dev lint format check publish redis-start redis-stop

install-dev:
	uv sync --all-extras --dev

# Start Redis for local testing
redis-start:
	docker run -d --name fastloop-test-redis -p 6379:6379 redis:7
	@echo "Redis started. Run 'make test' to run tests."

redis-stop:
	docker stop fastloop-test-redis && docker rm fastloop-test-redis

# Run tests (requires Redis - set REDIS_TEST_HOST=localhost or run 'make redis-start')
test:
	REDIS_TEST_HOST=localhost uv run pytest tests/ -v

test-verbose:
	REDIS_TEST_HOST=localhost uv run pytest tests/ -v -s

test-scheduling:
	REDIS_TEST_HOST=localhost uv run pytest tests/test_scheduling.py -v -s

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

check: lint format-check test
	@echo "All checks passed!"

publish:
	rm -rf dist/
	uv build
	uv run twine check dist/*
	uv run twine upload dist/*$(VERSION)*