"""Streamlit UI for Discord Traders.

Milestone 2C.2: manual raw-message entry. The user pastes raw trade message
text, the existing Milestone 2C.1 parser (app.parser.parse_message) is
invoked, and the structured result is displayed for review. No persistence
occurs here - no TradeService, repository, or database calls.
"""

from __future__ import annotations

import streamlit as st

from app.parser import parse_message

st.title("Discord Traders - Manual Message Entry")

raw_text = st.text_area("Paste raw trade message text", height=200)

if st.button("Parse Message"):
    signals = parse_message(raw_text)

    if not signals:
        st.warning("No trade signals found in this message.")
    else:
        st.success(f"Found {len(signals)} trade signal(s).")
        for signal in signals:
            st.json(signal)
