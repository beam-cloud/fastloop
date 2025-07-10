publish:
	uv build
	uv run twine check dist/*
	uv run twine upload dist/*