#!/bin/bash
# Rebuild the virtual environment from scratch.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
echo
echo "done. run ./jarvis-run"
echo "grant Accessibility + Camera to your terminal, and Microphone to Chrome —"
echo "see README.md > First run."
