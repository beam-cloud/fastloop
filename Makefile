VERSION=$(shell grep '^version =' pyproject.toml | head -1 | cut -d'"' -f2)

.PHONY: test test-verbose install-dev lint format check publish

install-dev:
	uv sync --all-extras --dev

test:
	uv run pytest tests/ -v

test-verbose:
	uv run pytest tests/ -v -s

test-scheduling:
	uv run pytest tests/test_scheduling.py -v -s

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