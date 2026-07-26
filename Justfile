set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Run the Python test suite.
test-huroshiki:
    PYTHONPATH=shared/scripts python -m unittest discover -s tests -v

# Run lightweight repository validation.
check:
    PYTHONPATH=shared/scripts python shared/scripts/packctl.py validate
    bash -n shared/scripts/huroshiki-launcher.sh
