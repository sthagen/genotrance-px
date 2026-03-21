VERSION := $(shell grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)
TAG := genotrance/px

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

.PHONY: test-musl
test-musl: ## Build and test in musl (Alpine) containers
	@. ./build.sh && build_local musl

.PHONY: test-glibc
test-glibc: ## Build and test in glibc (manylinux) containers
	@. ./build.sh && build_local glibc

.PHONY: docker
docker: ## Build local Docker images (full and mini)
	docker build -f docker/Dockerfile --build-arg BUILDER=local \
		--target mini -t $(TAG):$(VERSION)-mini -t $(TAG):latest-mini .
	docker build -f docker/Dockerfile --build-arg BUILDER=local \
		-t $(TAG):$(VERSION) -t $(TAG):latest .

.PHONY: test-kerberos
test-kerberos: docker ## Run Kerberos integration tests against a local KDC in Docker
	@uv run python -m pytest tests/test_kerberos.py -m integration -v

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
