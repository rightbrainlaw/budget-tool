"""Mint for James — Step 2: Google login + a first real (authenticated) read/write.

Flow:
  * Not signed in  -> show "Sign in with Google" (Supabase OAuth).
  * Google sends the browser back to http://localhost:8501/?code=... .
  * We exchange that code for a session, so the database now knows who you are.
  * Signed in -> greet by name, show YOUR budgets, let you create one.

Session note: the Supabase session + PKCE verifier are stored in the visitor's
own browser cookies (via streamlit-cookies-controller), so each user has an
independent session -- which is what makes multi-user hosting safe.

Run:  streamlit run app.py
"""

import base64
import json

import altair as alt
import pandas as pd
import streamlit as st
from streamlit_cookies_controller import CookieController

import normalizer

try:
    from supabase import create_client, ClientOptions
except ImportError:  # older/newer package layouts
    from supabase import create_client
    from supabase.lib.client_options import ClientOptions

st.set_page_config(page_title="Mint for James", page_icon="💰")

APP_URL = st.secrets.get("app_url", "http://localhost:8501")
SECURE_COOKIES = APP_URL.startswith("https")

# --- credentials ------------------------------------------------------------
try:
    URL = st.secrets["supabase"]["url"]
    KEY = st.secrets["supabase"]["anon_key"]
except Exception:
    st.error("Missing Supabase credentials in .streamlit/secrets.toml.")
    st.stop()


# --- per-browser session storage backed by cookies --------------------------
# Each visitor's Supabase session lives in THEIR OWN browser's cookies, so
# multiple users never share one session. (A single server-side file would be
# shared by everyone at once -- that's why the local-file version can't ship.)
cookies = CookieController()


class CookieStorage:
    def get_item(self, key):
        try:
            return cookies.get(key)
        except Exception:
            return None

    def set_item(self, key, value):
        try:
            cookies.set(
                key, value, max_age=60 * 60 * 24 * 30,
                secure=SECURE_COOKIES, same_site="lax",
            )
        except Exception:
            pass

    def remove_item(self, key):
        try:
            cookies.remove(key)
        except Exception:
            pass


def make_client():
    # A fresh client per run (NOT cached globally) so users on the server don't
    # share one client instance; the cookie storage scopes each to its browser.
    return create_client(
        URL, KEY,
        options=ClientOptions(flow_type="pkce", storage=CookieStorage()),
    )


supabase = make_client()

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
    """(access, refresh) from the client session, falling back to scanning the
    browser cookies, because supabase-py doesn't always populate the in-memory
    session when it's restored from storage on a fresh run."""
    try:
        sess = supabase.auth.get_session()
        if sess and getattr(sess, "access_token", None):
            return sess.access_token, getattr(sess, "refresh_token", None)
    except Exception:
        pass
    try:
        for value in (cookies.getAll() or {}).values():
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
header_left, header_right = st.columns([3, 1])
header_left.title("💰 Mint for James")

if not user:
    st.write("Sign in to see your budget.")
    oauth = supabase.auth.sign_in_with_oauth(
        {"provider": "google", "options": {"redirect_to": APP_URL}}
    )
    st.link_button("Sign in with Google", oauth.url, type="primary")
    st.stop()

name = (user.user_metadata or {}).get("full_name") or user.email
with header_right:
    st.caption(f"Signed in as **{name}**")
    if st.button("Sign out", use_container_width=True):
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
        # Clear this browser's cookies so the session is fully gone.
        try:
            for cookie_key in list((cookies.getAll() or {}).keys()):
                cookies.remove(cookie_key)
        except Exception:
            pass
        st.query_params.clear()
        st.rerun()

st.divider()

# --- Which budget? (the chooser + management live at the BOTTOM) ------------
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

budget_ids = [b["id"] for b in budgets]
if st.session_state.get("budget_id") not in budget_ids:
    st.session_state["budget_id"] = budget_ids[0]
budget_id = st.session_state["budget_id"]
budget_row = next(b for b in budgets if b["id"] == budget_id)
budget_name = budget_row["name"]
# The creator of a budget is its owner; only the owner can delete it or manage
# members. (RLS enforces this too -- this flag just controls what the UI shows.)
is_owner = budget_row.get("created_by") == auth_uid
st.caption(f"Budget: **{budget_name}**" + ("  ·  _owner_" if is_owner else "  ·  _member_"))

# --- Category data (loaded up here because the transactions grid needs it) --
categories = (
    supabase.table("category")
    .select("id,name,color")
    .eq("budget_id", budget_id)
    .order("name")
    .execute()
    .data
)
if not categories:
    # Seed a starter set the first time. Every one is editable/removable/recolorable.
    seed = [
        ("Groceries", "#2E7D32"), ("Dining", "#EF6C00"),
        ("Transportation", "#1565C0"), ("Utilities", "#6A1B9A"),
        ("Shopping", "#AD1457"), ("Subscriptions", "#00838F"),
        ("Income", "#00695C"), ("Transfer", "#757575"),
    ]
    supabase.table("category").insert(
        [{"budget_id": budget_id, "name": n, "color": col} for n, col in seed]
    ).execute()
    st.rerun()

DEFAULT_COLOR = "#888888"
color_by_name = {c["name"]: (c.get("color") or DEFAULT_COLOR) for c in categories}

# --- Transactions & reports (the main view) --------------------------------
st.divider()

txns = (
    supabase.table("transaction")
    .select("txn_id,date,amount,description,account")
    .eq("budget_id", budget_id)
    .order("date", desc=True)
    .limit(2000)
    .execute()
    .data
)
enrich_rows = (
    supabase.table("enrichment")
    .select("txn_id,category_id,reviewed")
    .eq("budget_id", budget_id)
    .execute()
    .data
)
enr = {e["txn_id"]: e for e in enrich_rows}
name_by_id = {c["id"]: c["name"] for c in categories}
id_by_name = {c["name"]: c["id"] for c in categories}
cat_options = [""] + sorted(id_by_name)


def cat_name(txn_id):
    return name_by_id.get(enr.get(txn_id, {}).get("category_id")) or ""


def is_reviewed(txn_id):
    return bool(enr.get(txn_id, {}).get("reviewed"))


COLS = {
    "txn_id": None,
    "date": st.column_config.TextColumn("Date", disabled=True),
    "description": st.column_config.TextColumn("Description", disabled=True, width="large"),
    "amount": st.column_config.NumberColumn("Amount", disabled=True, format="$%.2f"),
    "category": st.column_config.SelectboxColumn("Category", options=cat_options),
}

review = [t for t in txns if not is_reviewed(t["txn_id"])]
done = [t for t in txns if is_reviewed(t["txn_id"])]

tab_review, tab_done, tab_reports = st.tabs(
    [f"🔍 For review ({len(review)})", f"✅ Categorized ({len(done)})", "📊 Reports"]
)

with tab_review:
    if not txns:
        st.caption("No transactions yet — import a Chase CSV below.")
    elif not review:
        st.success("All caught up — nothing to review. 🎉")
    else:
        st.caption("Pick a category on each row, then Confirm to file it away.")
        rgrid = pd.DataFrame(
            [
                {
                    "txn_id": t["txn_id"],
                    "date": t["date"],
                    "description": t["description"],
                    "amount": float(t["amount"]),
                    "category": cat_name(t["txn_id"]),
                }
                for t in review
            ]
        )
        redit = st.data_editor(
            rgrid, key=f"review_{budget_id}_{len(rgrid)}",
            hide_index=True, use_container_width=True, column_config=COLS,
        )
        if st.button("Confirm categorized rows", type="primary", key="confirm_review"):
            n = 0
            for i in range(len(rgrid)):
                newcat = redit.iloc[i]["category"]
                if newcat:  # only file rows that actually got a category
                    supabase.table("enrichment").upsert(
                        {
                            "budget_id": budget_id,
                            "txn_id": rgrid.iloc[i]["txn_id"],
                            "category_id": id_by_name.get(newcat),
                            "reviewed": True,
                            "updated_by": auth_uid,
                        },
                        on_conflict="budget_id,txn_id",
                    ).execute()
                    n += 1
            st.success(f"Filed {n} transaction(s).")
            st.rerun()

with tab_done:
    if not done:
        st.caption("Nothing categorized yet — confirm some in the For review tab.")
    else:
        st.caption("Change a category, or untick Reviewed to send a row back to review.")
        dgrid = pd.DataFrame(
            [
                {
                    "txn_id": t["txn_id"],
                    "date": t["date"],
                    "description": t["description"],
                    "amount": float(t["amount"]),
                    "category": cat_name(t["txn_id"]),
                    "reviewed": True,
                }
                for t in done
            ]
        )
        dedit = st.data_editor(
            dgrid, key=f"done_{budget_id}_{len(dgrid)}",
            hide_index=True, use_container_width=True,
            column_config={**COLS, "reviewed": st.column_config.CheckboxColumn("Reviewed")},
        )
        if st.button("Save changes", type="primary", key="save_done"):
            n = 0
            for i in range(len(dgrid)):
                oc, nc = dgrid.iloc[i]["category"], dedit.iloc[i]["category"]
                orv, nrv = dgrid.iloc[i]["reviewed"], dedit.iloc[i]["reviewed"]
                if oc != nc or orv != nrv:
                    supabase.table("enrichment").upsert(
                        {
                            "budget_id": budget_id,
                            "txn_id": dgrid.iloc[i]["txn_id"],
                            "category_id": id_by_name.get(nc),
                            "reviewed": bool(nrv),
                            "updated_by": auth_uid,
                        },
                        on_conflict="budget_id,txn_id",
                    ).execute()
                    n += 1
            st.success(f"Updated {n} transaction(s).")
            st.rerun()

with tab_reports:
    st.caption("Only confirmed (categorized) transactions are counted.")
    rep_rows = [
        {
            "date": t["date"],
            "category": cat_name(t["txn_id"]) or "Uncategorized",
            "amount": float(t["amount"]),
        }
        for t in done
    ]
    if not rep_rows:
        st.info("Categorize some transactions to see reports.")
    else:
        rep = pd.DataFrame(rep_rows)
        rep["date"] = pd.to_datetime(rep["date"])

        # --- period filter ---
        period = st.radio(
            "Period", ["Month", "Year", "Custom range", "All time"],
            horizontal=True, key="rep_period",
        )
        if period == "Month":
            months = sorted(rep["date"].dt.strftime("%Y-%m").unique(), reverse=True)
            sel = st.selectbox("Month", months, key="rep_month")
            mask = rep["date"].dt.strftime("%Y-%m") == sel
            label = pd.to_datetime(sel + "-01").strftime("%B %Y")
        elif period == "Year":
            years = sorted(rep["date"].dt.year.unique(), reverse=True)
            sel = st.selectbox("Year", years, key="rep_year")
            mask = rep["date"].dt.year == sel
            label = str(sel)
        elif period == "Custom range":
            lo, hi = rep["date"].min().date(), rep["date"].max().date()
            c1, c2 = st.columns(2)
            start = c1.date_input("From", lo, key="rep_start")
            end = c2.date_input("To", hi, key="rep_end")
            mask = (rep["date"].dt.date >= start) & (rep["date"].dt.date <= end)
            label = f"{start:%b %d, %Y} – {end:%b %d, %Y}"
        else:
            mask = pd.Series(True, index=rep.index)
            label = "all time"

        view = rep[mask]
        st.caption(f"Showing **{label}** — {len(view)} transaction(s)")

        if view.empty:
            st.info("No confirmed transactions in this period.")
        else:
            out = -view.loc[view["amount"] < 0, "amount"].sum()
            inc = view.loc[view["amount"] > 0, "amount"].sum()
            m1, m2 = st.columns(2)
            m1.metric("Money out", f"${out:,.2f}")
            m2.metric("Money in", f"${inc:,.2f}")

            by_cat = (
                view.groupby("category")["amount"]
                .agg(total="sum", count="count")
                .reset_index()
                .sort_values("total")
            )
            st.dataframe(
                by_cat, hide_index=True, use_container_width=True,
                column_config={
                    "category": "Category",
                    "total": st.column_config.NumberColumn("Total", format="$%.2f"),
                    "count": st.column_config.NumberColumn("#"),
                },
            )
            spend = view[view["amount"] < 0].copy()
            if not spend.empty:
                spend["spent"] = -spend["amount"]
                by_spend = (
                    spend.groupby("category")["spent"].sum()
                    .sort_values(ascending=False).reset_index()
                )
                names = list(by_spend["category"])
                colors = [color_by_name.get(n, DEFAULT_COLOR) for n in names]
                chart = (
                    alt.Chart(by_spend)
                    .mark_bar()
                    .encode(
                        x=alt.X("spent:Q", title="Spent ($)", axis=alt.Axis(format="$,.0f")),
                        y=alt.Y("category:N", sort=names, title=None),
                        color=alt.Color(
                            "category:N",
                            scale=alt.Scale(domain=names, range=colors),
                            legend=None,
                        ),
                        tooltip=["category", alt.Tooltip("spent:Q", format="$,.2f")],
                    )
                )
                st.altair_chart(chart, use_container_width=True)

# --- Categories (manage) ----------------------------------------------------
st.divider()
st.subheader("Categories")
with st.expander("Manage categories"):
    add1, add2 = st.columns([4, 1])
    new_cat = add1.text_input(
        "Add a category", key="new_cat",
        label_visibility="collapsed", placeholder="New category name",
    )
    if add2.button("Add") and new_cat.strip():
        supabase.table("category").insert(
            {"budget_id": budget_id, "name": new_cat.strip(), "color": DEFAULT_COLOR}
        ).execute()
        st.rerun()
    st.caption("Edit a name to rename it; pick its color; 🗑 deletes it (and un-tags its transactions).")
    for c in categories:
        col1, col2, col3 = st.columns([5, 1, 1])
        renamed = col1.text_input(
            "name", value=c["name"], key=f"cat_{c['id']}", label_visibility="collapsed"
        )
        picked = col2.color_picker(
            "color", value=c.get("color") or DEFAULT_COLOR,
            key=f"color_{c['id']}", label_visibility="collapsed",
        )
        if renamed.strip() and renamed != c["name"]:
            supabase.table("category").update(
                {"name": renamed.strip()}
            ).eq("id", c["id"]).execute()
            st.rerun()
        if picked != (c.get("color") or DEFAULT_COLOR):
            supabase.table("category").update(
                {"color": picked}
            ).eq("id", c["id"]).execute()
            st.rerun()
        if col3.button("🗑", key=f"del_{c['id']}"):
            supabase.table("category").delete().eq("id", c["id"]).execute()
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

# --- Budgets: switch / create / delete (kept at the bottom, out of the way) --
st.divider()
with st.expander("Budgets — switch, create, or delete"):
    st.selectbox(
        "Active budget",
        options=budget_ids,
        format_func=lambda bid: next(b["name"] for b in budgets if b["id"] == bid),
        key="budget_id",
    )

    st.markdown("**Create a new budget**")
    with st.form("new_budget"):
        new_name = st.text_input("Name")
        if st.form_submit_button("Create") and new_name.strip():
            supabase.table("budget").insert(
                {"name": new_name.strip(), "created_by": auth_uid or user.id}
            ).execute()
            st.rerun()

    if is_owner:
        st.markdown("**Delete this budget**")
        st.warning(
            f"Permanently deletes “{budget_name}” and ALL its transactions, "
            "categories, and members. This cannot be undone."
        )
        if st.checkbox(f"Yes, delete “{budget_name}” and everything in it"):
            if st.button("Delete budget", type="primary"):
                supabase.table("budget").delete().eq("id", budget_id).execute()
                st.session_state.pop("budget_id", None)
                st.rerun()
    else:
        st.caption("Only the budget's owner can delete it.")

# --- Members: who can access this budget ------------------------------------
st.divider()
with st.expander("Members — who can access this budget"):
    members = (
        supabase.table("budget_member")
        .select("user_id,role,added_at")
        .eq("budget_id", budget_id)
        .execute()
        .data
    )
    member_ids = [m["user_id"] for m in members]
    profs = (
        supabase.table("profile")
        .select("id,email,display_name")
        .in_("id", member_ids)
        .execute()
        .data
        if member_ids
        else []
    )
    prof_by_id = {p["id"]: p for p in profs}

    st.markdown("**Current members**")
    for m in members:
        p = prof_by_id.get(m["user_id"], {})
        label = p.get("display_name") or p.get("email") or m["user_id"]
        role = m.get("role") or "member"
        tags = (["you"] if m["user_id"] == auth_uid else []) + [role]
        c1, c2 = st.columns([4, 1])
        c1.write(f"{label}  ·  _{', '.join(tags)}_")
        # Owner-only removal, and the owner can never be removed.
        if is_owner and role != "owner" and c2.button("Remove", key=f"rm_{m['user_id']}"):
            supabase.table("budget_member").delete().eq("budget_id", budget_id).eq(
                "user_id", m["user_id"]
            ).execute()
            st.rerun()

    if is_owner:
        st.markdown("**Add a member by email**")
        st.caption("They must have signed into the app at least once first (so an account exists).")
        a1, a2 = st.columns([4, 1])
        invite_email = a1.text_input(
            "email", key="invite_email",
            label_visibility="collapsed", placeholder="person@example.com",
        )
        if a2.button("Add", key="add_member") and invite_email.strip():
            try:
                res = supabase.rpc(
                    "add_member_by_email",
                    {"p_budget_id": budget_id, "p_email": invite_email.strip()},
                ).execute()
                status = res.data
                if status == "ok":
                    st.success("Member added.")
                    st.rerun()
                elif status == "user_not_found":
                    st.warning("No account with that email yet — have them sign in once, then try again.")
                elif status == "not_authorized":
                    st.error("You don't have permission to add members to this budget.")
                else:
                    st.error(f"Unexpected result: {status}")
            except Exception as e:
                st.error("Couldn't add member — is the add_member_by_email function installed in the database?")
                st.exception(e)
    else:
        st.caption("Only the budget's owner can add or remove members.")
