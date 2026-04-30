"""Backward-compatible alias: structure_lint -> fingerprint_lint.

The module was renamed from structure_lint.py to fingerprint_lint.py
to avoid shadowing app/lint.py (the spintax linter). This thin re-export
keeps any existing imports working.
"""

from app.tools.fingerprint_lint import lint_structure_repetition  # noqa: F401

__all__ = ["lint_structure_repetition"]
