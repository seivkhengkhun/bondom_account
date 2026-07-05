"""Reflex configuration for the admin panel.

The project root is added to sys.path so the `shared` package (single
source of truth for models/db/services) resolves when Reflex runs from
`app/web/`.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import reflex as rx

# Local defaults: frontend 3000, backend 8001.
# In managed platforms, these can be overridden with env vars.
frontend_port = int(os.getenv("FRONTEND_PORT", "3000"))
backend_port = int(os.getenv("BACKEND_PORT", "8001"))

config = rx.Config(
    app_name="admin",
    frontend_port=frontend_port,
    backend_port=backend_port,
)
