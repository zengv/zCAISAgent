PYTHON ?= python
export PYTHONPATH := src

.PHONY: dev test smoke eval eval-gate lint typecheck

dev:
	uvicorn zcaisagent.api.app:app --reload --host 127.0.0.1 --port 8080

test:
	$(PYTHON) -m pytest

smoke:
	$(PYTHON) -m pytest tests/smoke -q

eval:
	$(PYTHON) -m zcaisagent.cli eval data/eval/golden.jsonl

eval-gate:
	$(PYTHON) -m zcaisagent.cli eval-gate --path data/eval/golden.jsonl

lint:
	ruff check src tests

typecheck:
	mypy src
