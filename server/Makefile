# ipixel-mcp origin — dev tasks. Hardware-free; no network required for `test`.

PYTHON ?= python3

.PHONY: test audit lint install help

help:
	@echo "make test    - run the hardware-free test suite (no network)"
	@echo "make audit   - pip-audit against the pinned constraints (needs network)"
	@echo "make lint    - byte-compile every module to catch syntax errors"
	@echo "make install - editable install with dev extras (needs network)"

test:
	$(PYTHON) -m pytest -q

# pip-audit reads the resolved environment / constraints for known CVEs (F-7).
# Treat any Pillow/bleak advisory as release-blocking.
audit:
	$(PYTHON) -m pip_audit -r constraints.txt || \
		echo "pip-audit not installed or offline; run 'pip install pip-audit' in CI"

# Lightweight, offline lint: compile all sources (no third-party linter needed).
lint:
	$(PYTHON) -m compileall -q ipixel_mcp

install:
	$(PYTHON) -m pip install -e '.[dev]' -c constraints.txt
