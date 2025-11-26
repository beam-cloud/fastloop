VERSION=$(shell grep '^version =' pyproject.toml | head -1 | cut -d'"' -f2)

.PHONY: test test-verbose install-dev publish

install-dev:
	uv pip install -e ".[dev]"

test:
	uv run pytest tests/ -v

test-verbose:
	uv run pytest tests/ -v -s

test-scheduling:
	uv run pytest tests/test_scheduling.py -v -s

publish:
	rm -rf dist/
	uv build
	uv run twine check dist/*
	uv run twine upload dist/*$(VERSION)*