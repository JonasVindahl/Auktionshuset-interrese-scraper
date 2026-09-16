"""Webdashboard for Auktionshuset Hunter.

``serve`` importeres dovent, så agenten selv kan køre uden FastAPI installeret.
Kun ``auction_hunter web`` har brug for web-afhængighederne.
"""

from __future__ import annotations


def serve(**kwargs: object) -> None:
    from .app import serve as _serve
    _serve(**kwargs)  # type: ignore[arg-type]


__all__ = ["serve"]
