"""Discord Python code-execution bot.

A lightweight bot that ONLY executes code found inside explicitly marked
```python / ```py fenced code blocks. Everything else is ignored.

Designed for Render's free tier: single process, in-memory state, no external
services (no Redis/DB), and very low resource usage.
"""

__version__ = "1.0.0"
