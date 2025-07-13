VERSION=$(shell grep '^version =' pyproject.toml | head -1 | cut -d'"' -f2)

publish:
	rm -rf dist/
	uv build
	uv run twine check dist/*
	uv run twine upload dist/*$(VERSION)*