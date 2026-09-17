# Hyppige kommandoer. Alt kører gennem .venv, så versioner og pakker er de
# samme som i CI og i containeren.
VENV ?= .venv
BIN  := $(VENV)/bin
PY   := $(BIN)/python
PIP  := $(BIN)/pip
RUFF := $(BIN)/ruff

IMAGE_TAG ?= latest
IMAGE_VERSION ?= 0.0.0

.PHONY: help venv install test lint fmt run once scan check stats backup up down logs image ci-install clean

help:
	@echo "make venv      opret virtuel miljoe og installer afhaengigheder"
	@echo "make test      hele testsuiten"
	@echo "make lint      ruff paa src og tests"
	@echo "make run       agenten i loop (15 min)"
	@echo "make once      én rigtig koersel"
	@echo "make scan      vis fund uden at gemme eller sende"
	@echo "make check     konfiguration og Discord-test"
	@echo "make backup    konsistent backup af databasen til backups/"
	@echo "make image     byg docker-imaget med version og revision"
	@echo "make up/down   start eller stop compose"
	@echo "make ci-install kopier ci/ci.yml ind i .github/workflows/"

venv:
	$(PY) -m venv $(VENV) 2>/dev/null || python3 -m venv $(VENV)

install: venv
	$(PIP) install -r requirements.txt pytest ruff "httpx2>=2.13"

test:
	$(PY) -m pytest

lint:
	$(RUFF) check src tests

fmt:
	$(RUFF) check src tests --fix

run:
	$(PY) -m auction_hunter run

once:
	$(PY) -m auction_hunter once

scan:
	$(PY) -m auction_hunter scan

check:
	$(PY) -m auction_hunter check

stats:
	$(PY) -m auction_hunter stats

backup:
	$(PY) -m auction_hunter backup --out backups

image:
	IMAGE_TAG=$(IMAGE_TAG) IMAGE_VERSION=$(IMAGE_VERSION) \
	IMAGE_REVISION=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown) \
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ci-install:
	@mkdir -p .github/workflows
	cp ci/ci.yml .github/workflows/ci.yml
	@echo "CI-workflow lagt i .github/workflows/ci.yml. Commit og push med et"
	@echo "token der har workflow-scope (gh auth refresh -s workflow)."

clean:
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
