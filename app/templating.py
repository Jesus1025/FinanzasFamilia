"""Jinja2 templates compartidos, con filtros utiles."""
from __future__ import annotations

import os

from fastapi.templating import Jinja2Templates

from .config import config
from .money import format_clp

_BASE = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(_BASE, "templates"))
templates.env.filters["clp"] = format_clp
templates.env.globals["config"] = config
