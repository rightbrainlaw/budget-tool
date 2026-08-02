"""Mint for James — Step 2: Google login + a first real (authenticated) read/write.

Flow:
  * Not signed in  -> show "Sign in with Google" (Supabase OAuth).
  * Google sends the browser back to http://localhost:8501/?code=... .
  * We exchange that code for a session, so the database now knows who you are.
  * Signed in -> greet by name, show YOUR budgets, let you create one.

Local-dev note: the Supabase session + PKCE verifier are cached in a small
local JSON file so they survive the redirect out to Google and back. That file
is git-ignored and dev-only; a deployed version would use browser cookies
instead (a single shared file is fine for one person on one machine, wrong for
many users on a server).

Run:  streamlit run app.py
"""

import json
import os

import streamlit as st

try:
    from supabase import create_client, ClientOptions
except ImportError:  # older/newer package layouts
    from supabase import create_client
    from supabase.lib.client_options import ClientOptions

st.set_page_config(page_title="Mint for James", page_icon="💰")

REDIRECT_URL = "http://localhost:8501"
AUTH_STORE = ".streamlit/.auth_store.json"

# --- credentials ------------------------------------------------------------
try:
    URL = st.secrets["supabase"]["url"]
    KEY = st.secrets["supabase"]["anon_key"]
except Exception:
    st.error("Missing Supabase credentials in .streamlit/secrets.toml.")
    st.stop()


# --- a tiny file-backed storage so the session survives the OAuth redirect ---
class FileStorage:
    def __init__(self, path):
        self.path = path

    def _read(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _write(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(data, f)

    def get_item(self, key):
        return self._read().get(key)

    def set_item(self, key, value):
        data = self._read()
        data[key] = value
        self._write(data)

    def remove_item(self, key):
        data = self._read()
        data.pop(key, None)
        self._write(data)


@st.cache_resource
def get_client():
    return create_client(
        URL, KEY,
        options=ClientOptions(flow_type="pkce", storage=FileStorage(AUTH_STORE)),
    )


supabase = get_client()

# --- 1. Handle the redirect back from Google (?code=...) ---------------------
code = st.query_params.get("code")
if code:
    try:
        supabase.auth.exchange_code_for_session({"auth_code": code})
    except Exception as e:
        st.error("Couldn't complete sign-in (code exchange failed).")
        st.exception(e)
    st.query_params.clear()
    st.rerun()

# --- 2. Who is signed in? ---------------------------------------------------
def current_user():
    try:
        resp = supabase.auth.get_user()
        return resp.user if resp else None
    except Exception:
        return None


user = current_user()

# Make sure DB queries run *as* the signed-in user (so RLS sees auth.uid()).
try:
    session = supabase.auth.get_session()
    if session:
        supabase.postgrest.auth(session.access_token)
except Exception:
    pass

# --- 3. Render --------------------------------------------------------------
st.title("💰 Mint for James")

if not user:
    st.write("Sign in to see your budget.")
    oauth = supabase.auth.sign_in_with_oauth(
        {"provider": "google", "options": {"redirect_to": REDIRECT_URL}}
    )
    st.link_button("Sign in with Google", oauth.url, type="primary")
    st.stop()

name = (user.user_metadata or {}).get("full_name") or user.email
st.success(f"Signed in as {name}")
if st.button("Sign out"):
    supabase.auth.sign_out()
    st.query_params.clear()
    st.rerun()

st.divider()
st.subheader("Your budgets")
budgets = supabase.table("budget").select("*").order("created_at").execute().data
if budgets:
    for b in budgets:
        st.write(f"• {b['name']}")
else:
    st.caption("None yet — create your first one below.")

with st.form("new_budget"):
    bname = st.text_input("Budget name", "Mint for James")
    if st.form_submit_button("Create budget"):
        supabase.table("budget").insert(
            {"name": bname, "created_by": user.id}
        ).execute()
        st.rerun()
