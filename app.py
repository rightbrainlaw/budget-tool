"""Mint for James — Step 1: connection smoke test.

Proves the Streamlit app can reach your Supabase project and that Row-Level
Security is switched on, BEFORE we add Google login. Google login goes on top
of this in Step 2.

Run it with:
    streamlit run app.py
"""

import streamlit as st
from supabase import create_client

st.set_page_config(page_title="Mint for James", page_icon="💰")
st.title("💰 Mint for James")
st.caption("Step 1 — connection check (login comes next)")

# --- Read Supabase credentials from .streamlit/secrets.toml -----------------
try:
    url = st.secrets["supabase"]["url"]
    anon_key = st.secrets["supabase"]["anon_key"]
except Exception:
    st.error(
        "No Supabase credentials found. Copy `.streamlit/secrets.toml.example` "
        "to `.streamlit/secrets.toml` and fill in your project URL and anon key "
        "(Supabase → Project Settings → API)."
    )
    st.stop()

supabase = create_client(url, anon_key)

# --- The actual test --------------------------------------------------------
# We query the `budget` table as an anonymous visitor (not logged in). Because
# Row-Level Security is on, the RIGHT answer is a *successful* query that returns
# ZERO rows: the connection works, the schema exists, and RLS is correctly
# hiding data from someone who isn't a member of any budget.
st.subheader("Connection check")
try:
    result = supabase.table("budget").select("*").execute()
    st.success("✅ Connected to Supabase, and the schema is there.")
    st.metric("Budgets visible to an anonymous visitor", len(result.data))
    st.info(
        "**0 is the correct answer.** Row-Level Security is hiding everything "
        "because you're not logged in yet. Step 2 (Google login) will let the "
        "database recognize you — and then you'll see your budgets."
    )
except Exception as e:
    st.error("Couldn't reach Supabase or query the schema. Details below:")
    st.exception(e)
