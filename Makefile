VENV := mobile_agent/.venv
PYTHON ?= $(VENV)/bin/python
# The interpreter that creates the venv. macOS's own python3 is 3.9; Mobster needs 3.12+.
PYTHON_BOOTSTRAP ?= python3.12

.PHONY: test test-backend test-frontend venv screenshots

# The dashboard's tests run only where the dashboard is checked out.
test: test-backend $(if $(wildcard dashboard/package.json),test-frontend)

test-backend:
	$(PYTHON) -m unittest discover -s mobile_agent -t .

test-frontend:
	cd dashboard && npm test

venv:
	@$(PYTHON_BOOTSTRAP) -c 'import sys; assert sys.version_info >= (3, 12), "Mobster needs Python 3.12+ (brew install python@3.12, or uv python install 3.12)"' \
		|| { echo "Set PYTHON_BOOTSTRAP to a Python 3.12+ interpreter, e.g. make venv PYTHON_BOOTSTRAP=/opt/homebrew/bin/python3.12" >&2; exit 1; }
	$(PYTHON_BOOTSTRAP) -m venv $(VENV)
	$(VENV)/bin/pip install -r mobile_agent/requirements.txt

# The terminal UI rendered headlessly in each state (SVG, plus PNG through headless Chrome).
SHOTS ?= /tmp/mobster-shots
screenshots:
	$(PYTHON) scripts/tui_screenshots.py $(SHOTS) --png
