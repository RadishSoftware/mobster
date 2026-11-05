VENV := mobile_agent/.venv
PYTHON ?= $(VENV)/bin/python
PYTHON_BOOTSTRAP ?= python3.12
TEST_JOBS ?= 1

.PHONY: test test-backend venv lint
test: test-backend

test-backend:
	nice -n 10 $(PYTHON) scripts/ci_backend.py --jobs $(TEST_JOBS)

venv:
	$(PYTHON_BOOTSTRAP) -m venv $(VENV)
	$(VENV)/bin/pip install -r mobile_agent/requirements.txt -r mobile_agent/requirements-dev.txt

lint:
	$(VENV)/bin/ruff check mobile_agent scripts
