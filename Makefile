# GSD Bridge development convenience targets

PYTHON ?= python3
TESTS  ?= tests

.PHONY: test lint typecheck ci smoke setup reset clean

## test: Run unit tests
test:
	$(PYTHON) -m unittest discover -s $(TESTS) -p "test_*.py" -v

## lint: Run ruff linter
lint:
	ruff check gsd_bridge $(TESTS)

## typecheck: Run mypy
typecheck:
	mypy gsd_bridge

## ci: Run lint + typecheck + tests (mirrors CI workflow)
ci: lint typecheck test

## smoke: Quick sanity check (import + --version)
smoke:
	$(PYTHON) -c "import gsd_bridge; print('import ok:', gsd_bridge.__version__)"
	$(PYTHON) -m gsd_bridge --version

## setup: Install package in editable mode with dev deps
setup:
	pip install -e ".[dev]"

## reset: Clean caches and reinstall
reset: clean setup

## clean: Remove Python bytecode and tool caches
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .mypy_cache .ruff_cache *.egg-info dist build 2>/dev/null || true
	@echo "Cleaned."
