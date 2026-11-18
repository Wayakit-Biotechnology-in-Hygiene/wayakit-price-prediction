"""Infrastructure scripts for Price Intelligence (one script per AWS resource).

This is a package so the scripts can share `infra.config` and `infra._aws` without clashing
with `scraper/config.py` (both would be the top-level module `config` otherwise). Run the
scripts from the repo root as modules, for example:

    python -m infra.bucket --dry-run

Conventions: see "infra/ conventions" in CLAUDE.md. Importing any module in this package never
calls AWS.
"""
