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

import base64
import json
import os

import streamlit as st

import normalizer

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


def stored_tokens():
    """(access, refresh) from the client session, falling back to the session
    file, because supabase-py doesn't always populate the in-memory session when
    it's restored from storage in a fresh process."""
    try:
        sess = supabase.auth.get_session()
        if sess and getattr(sess, "access_token", None):
            return sess.access_token, getattr(sess, "refresh_token", None)
    except Exception:
        pass
    try:
        with open(AUTH_STORE) as f:
            data = json.load(f)
        for value in data.values():
            try:
                obj = json.loads(value) if isinstance(value, str) else value
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("access_token"):
                return obj.get("access_token"), obj.get("refresh_token")
    except Exception:
        pass
    return None, None


# Make DB queries run *as* the signed-in user (so RLS sees auth.uid()).
access, refresh = stored_tokens()
if access and refresh:
    try:
        supabase.auth.set_session(access, refresh)
    except Exception:
        pass
if access:
    supabase.postgrest.auth(access)


def token_uid(token):
    """The `sub` (user id) claim inside the JWT access token -- exactly what the
    database sees as auth.uid(), so it's the safest id to write as created_by."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except Exception:
        return None


auth_uid = token_uid(access) if access else None

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

# --- Pick or create a budget ------------------------------------------------
budgets = supabase.table("budget").select("*").order("created_at").execute().data

if not budgets:
    st.subheader("Create your first budget")
    with st.form("first_budget"):
        bname = st.text_input("Budget name", "Mint for James")
        if st.form_submit_button("Create budget"):
            supabase.table("budget").insert(
                {"name": bname, "created_by": auth_uid or user.id}
            ).execute()
            st.rerun()
    st.stop()

budget_id = st.selectbox(
    "Budget",
    options=[b["id"] for b in budgets],
    format_func=lambda bid: next(b["name"] for b in budgets if b["id"] == bid),
)

with st.expander("＋ New budget"):
    with st.form("new_budget"):
        new_name = st.text_input("Name")
        if st.form_submit_button("Create") and new_name.strip():
            supabase.table("budget").insert(
                {"name": new_name.strip(), "created_by": auth_uid or user.id}
            ).execute()
            st.rerun()

# --- Import a Chase CSV -----------------------------------------------------
st.divider()
st.subheader("Import transactions")
account = st.text_input(
    "Account name",
    "chase-checking",
    help="A label for which account this file is (e.g. 'chase-checking-9266'). "
    "Used to tell accounts apart and to catch transfers between them.",
)
uploaded = st.file_uploader("Chase CSV export", type="csv")
if uploaded is not None and st.button("Import", type="primary"):
    try:
        uploaded.seek(0)
        df = normalizer.normalize_file(uploaded, account, source_file=uploaded.name)
        records = [
            {
                "budget_id": budget_id,
                "txn_id": r["txn_id"],
                "date": r["date"].isoformat(),
                "posted_date": r["posted_date"].isoformat(),
                "amount": float(r["amount"]),
                "description": r["description"],
                "account": r["account"],
                "pending": bool(r["pending"]),
                "source_file": r["source_file"],
                "imported_by": auth_uid,
            }
            for _, r in df.iterrows()
        ]
        supabase.table("transaction").upsert(
            records, on_conflict="budget_id,txn_id"
        ).execute()
        st.success(f"Imported {len(records)} transactions from {uploaded.name}.")
    except Exception as e:
        st.error("Import failed.")
        st.exception(e)
        try:
            uploaded.seek(0)
            preview = uploaded.read().decode("utf-8", "replace")[:900]
            st.caption("First 900 characters of the uploaded file (for debugging):")
            st.code(preview)
        except Exception:
            pass

# --- Show the ledger --------------------------------------------------------
st.divider()
st.subheader("Transactions")
txns = (
    supabase.table("transaction")
    .select("date,amount,description,account,pending")
    .eq("budget_id", budget_id)
    .order("date", desc=True)
    .limit(1000)
    .execute()
    .data
)
if txns:
    st.caption(f"{len(txns)} transactions (newest first)")
    st.dataframe(txns, use_container_width=True, hide_index=True)
else:
    st.caption("No transactions yet — import a Chase CSV above.")
