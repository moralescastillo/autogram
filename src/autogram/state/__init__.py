"""State that lives in the user's storage, never in the repository.

- ``token.json``     — the live Instagram token and its expiry (§4)
- ``published.json`` — the ledger, checked before every publish (§8.1)
- ``log/``           — JSONL run records for downstream reporting (§8.2)
"""
