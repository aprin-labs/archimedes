"""Archimedes command-line interface.

The version here is the single source of truth. ``tests/test_cli.py`` asserts it
matches ``pyproject.toml`` so a release cannot ship a binary that reports one
number while PyPI shows another.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
