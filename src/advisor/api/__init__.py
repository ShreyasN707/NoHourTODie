"""HTTP API for the Destination Advisor (module B). Contract: docs/advisor_contract.md.

Run:  PYTHONPATH=src .venv/bin/uvicorn advisor.api.app:app --port 8000   (or `make advisor-api`)

Kept import-free on purpose: `advisor.api.state.DEMO_BANNER` can be imported without FastAPI installed.
"""
