"""raidbots_session.py — Centralised Raidbots HTTP session factory.

Single source of truth for session creation: sets shared headers, optional
raidsid cookie, and exposes a ``load_raidsid`` helper so callers never touch
config.json directly for session purposes.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from droptimizer import RAIDBOTS_HEADERS

_DEFAULT_CONFIG = Path(__file__).parent / "config.json"


def make_raidbots_session(
    raidsid: str | None = None,
    *,
    config_path: Path = _DEFAULT_CONFIG,
) -> requests.Session:
    """Return a :class:`requests.Session` configured for Raidbots.

    If *raidsid* is ``None`` (the default) the value is read from
    ``config_path`` (``config.json`` next to this file).  Pass an explicit
    empty string to skip the cookie entirely.

    Args:
        raidsid:     Raidbots session cookie value, or ``None`` to read from
                     config.
        config_path: Override the config file path (useful in tests).

    Returns:
        A :class:`requests.Session` with Raidbots headers and, when available,
        the ``raidsid`` cookie set on ``www.raidbots.com``.
    """
    if raidsid is None:
        try:
            raidsid = json.loads(config_path.read_text()).get("raidsid", "")
        except Exception:
            raidsid = ""

    s = requests.Session()
    s.headers.update(RAIDBOTS_HEADERS)
    if raidsid:
        s.cookies.set("raidsid", raidsid, domain="www.raidbots.com")
    return s
