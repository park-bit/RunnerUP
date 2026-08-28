"""Pytest configuration and shared fixtures.

Ensures the project root (which contains the ``app`` package) is importable no
matter where pytest is launched from.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
