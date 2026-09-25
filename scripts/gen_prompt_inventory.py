#!/usr/bin/env python3
"""Top-level wrapper for the prompt-inventory doc generator.

Exists so the documented command runs exactly as written::

    PYTHONPATH=backend python scripts/gen_prompt_inventory.py           # write the doc
    PYTHONPATH=backend python scripts/gen_prompt_inventory.py --check   # exit 1 if stale (CI)

Thin delegate to the real generator in the backend package
(``backend/archimedes/scripts/gen_prompt_inventory.py``); ``PYTHONPATH=backend``
makes ``archimedes`` importable. The module form
``python -m archimedes.scripts.gen_prompt_inventory`` keeps working too.
"""

from __future__ import annotations

from archimedes.scripts.gen_prompt_inventory import main

if __name__ == "__main__":
    raise SystemExit(main())
