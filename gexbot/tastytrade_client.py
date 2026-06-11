"""
Tastytrade Client — Shared session/account creation
=====================================================
Extracted from alfred_execution.py. Centralizes the Session + Account
pattern used by all async tastytrade functions.

Usage:
    from tastytrade_client import get_session_and_account
    session, account = await get_session_and_account()
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def get_session_and_account():
    """Create a tastytrade session and return (session, account).

    Uses TT_SECRET and TT_REFRESH from environment.
    Returns the first account on the session.
    """
    from tastytrade import Account, Session

    session = Session(
        provider_secret=os.environ.get("TT_SECRET"),
        refresh_token=os.environ.get("TT_REFRESH"),
        is_test=False,
    )
    accounts = await Account.get(session)
    account = accounts[0]
    return session, account
