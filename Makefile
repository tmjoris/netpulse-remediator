.PHONY: install lint fmt typecheck test check simulate serve up down e2e

PY ?= .venv/bin/python
BIN := $(dir $(PY))

install:            ## create .venv and install with dev tools
	python3 -m venv .venv && $(PY) -m pip install -e '.[dev]'

lint:
	$(BIN)ruff check . && $(BIN)ruff format --check .

fmt:
	$(BIN)ruff check --fix . && $(BIN)ruff format .

typecheck:
	$(BIN)mypy

test:
	$(BIN)pytest --cov

check: lint typecheck test   ## everything CI runs before the stack job

simulate:
	$(BIN)netpulse simulate --scenario all

serve:
	$(BIN)netpulse serve --config config/netpulse.example.toml

up:
	docker compose up --build -d --wait

down:
	docker compose down -v

e2e: up             ## drive every scenario through the containerised API
	$(BIN)netpulse simulate --scenario all --target http://127.0.0.1:8000
	docker compose exec -T netpulse netpulse audit verify /var/lib/netpulse/netpulse.audit.jsonl
