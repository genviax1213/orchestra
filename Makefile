PYTHON ?= python3
VENV ?= .venv

.PHONY: install test help venv

help:
	@printf "Targets:\n"
	@printf "  make venv     Create the local virtual environment\n"
	@printf "  make install  Install orchestra in editable mode into .venv\n"
	@printf "  make run      Run the local wrapper\n"
	@printf "  make test     Run the test suite\n"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(VENV)/bin/python -m pip install -e .

run:
	./bin/orchestra --help

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests
