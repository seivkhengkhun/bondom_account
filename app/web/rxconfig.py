"""Reflex configuration for the admin panel.

The project root is added to sys.path so the `shared` package (single
source of truth for models/db/services) resolves when Reflex runs from
`app/web/`.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import reflex as rx

# backend_port 8001: the FastAPI store API already occupies 8000.
config = rx.Config(
    app_name="admin",
    frontend_port=3000,
    backend_port=8001,
)
