"""Data providers for the extension.

``mightpulse`` supplies live player/alliance/kingdom-ranking data (Bearer API +
site-scrape refresh fallback) — the data source the upstream bot lost when the
game removed its player API. It is the extension's only live game-data source.
"""

from . import mightpulse
from .mightpulse import available as mightpulse_available
from .mightpulse import configure as configure_mightpulse

__all__ = ["mightpulse", "mightpulse_available", "configure_mightpulse"]

