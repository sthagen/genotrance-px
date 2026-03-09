.PHONY: install
install: ## Install the virtual environment and pre-commit hooks
	@uv sync
	@uv pip install -e . -f mcurllib
	@uv run python -m pre_commit install

.PHONY: check
check: ## Run code quality tools
	@uv run pre-commit run -a
	@uv run mypy

.PHONY: test
test: ## Run the test suite with coverage
	@uv run python -m pytest tests -n 4 --cov --cov-config=pyproject.toml --cov-report=xml

.PHONY: build
build: clean ## Build sdist and wheel
	@uv build

.PHONY: clean
clean: ## Remove build artifacts
	@rm -rf dist/ build/ *.egg-info px_proxy.egg-info
	@rm -f .coverage coverage.xml

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
