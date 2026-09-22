#!/usr/bin/env python3
"""Compatibility wrapper for the LatticeShadow benchmark harness."""

from latticeshadow_db.benchmarks import main


if __name__ == "__main__":
    raise SystemExit(main())
