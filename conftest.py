# cntrl: repo-wide pytest memory guard (scripts/cntrl/pytest_memguard.py).
# A bare multi-directory pytest run grew to 50 GB per process on 2026-09-16.
pytest_plugins = ["scripts.cntrl.pytest_memguard"]
