"""HTTP layer for the OpenClaw review UI.

A thin skin over the existing pipeline modules. Every endpoint delegates to a
function the CLI already uses, so the two entry points can never drift apart.
"""

from .app import create_app

__all__ = ["create_app"]
