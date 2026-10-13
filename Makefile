VENV := mobile_agent/.venv
PYTHON ?= $(VENV)/bin/python
# The interpreter that creates the venv. macOS's own python3 is 3.9; the engine needs 3.12+.
PYTHON_BOOTSTRAP ?= python3.12
TEST_JOBS ?= 4

.PHONY: test test-backend venv

test: test-backend

test-backend:
	$(PYTHON) scripts/ci_backend.py --jobs $(TEST_JOBS)

venv:
	@$(PYTHON_BOOTSTRAP) -c 'import sys; assert sys.version_info >= (3, 12), "The engine needs Python 3.12+ (brew install python@3.12, or uv python install 3.12)"' \
		|| { echo "Set PYTHON_BOOTSTRAP to a Python 3.12+ interpreter, e.g. make venv PYTHON_BOOTSTRAP=/opt/homebrew/bin/python3.12" >&2; exit 1; }
	$(PYTHON_BOOTSTRAP) -m venv $(VENV)
	$(VENV)/bin/pip install -r mobile_agent/requirements.txt
