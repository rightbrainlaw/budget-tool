"""Chase checking CSV -> one consistent internal ledger table.

Pass one (ledger layer). Implements SPEC-normalizer.md and nothing else: no UI,
no auth, no hosting. It turns a raw Chase export into the fixed ledger schema --
bank facts only, no interpretation. Meaning (merchant, category) is the
enrichment layer's job, joined back later by txn_id.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from datetime import date

import pandas as pd

# The exact header Chase writes, used to sanity-check inputs before we trust
# any column by name.
CHASE_HEADER = [
    "Details",
    "Posting Date",
    "Description",
    "Amount",
    "Type",
    "Balance",
    "Check or Slip #",
]

# The fixed ledger schema, in order. normalize_file() guarantees exactly these
# columns and nothing else -- bank facts only. merchant/category are NOT here;
# they are enrichment, joined back by txn_id. (Balance is dropped too -- see
# below.)
OUTPUT_COLUMNS = [
    "txn_id",
    "date",
    "posted_date",
    "amount",
    "description",
    "account",
    "pending",
    "source_file",
]


# --------------------------------------------------------------------------- #
# 1. Read
# --------------------------------------------------------------------------- #
def read_chase_csv(path: str) -> pd.DataFrame:
    """Read a raw Chase export with no type inference we haven't asked for.

    Notes forced by the spec:
      * Posting Date is read as string; we parse it later with an explicit
        format so pandas never guesses M/D/YY vs D/M/YY.
      * Amount is left to pandas as float (it is already signed).
      * Balance comes back as `object` (str) on real files because pending
        rows carry a literal single space -- we rely on that, not fight it.
      * keep_default_na leaves empty fields as NaN, which is how we detect the
        junk rows in the next step.
    """
    df = pd.read_csv(
        path,
        dtype={"Posting Date": "string", "Balance": "string"},
    )

    # Fail early if this isn't the schema we think it is. A credit-card export
    # (different columns) should be rejected here, not silently mangled.
    if list(df.columns) != CHASE_HEADER:
        raise ValueError(
            f"Unexpected header.\n  expected: {CHASE_HEADER}\n  got:      {list(df.columns)}"
        )
    return df


# --------------------------------------------------------------------------- #
# 2. Row filter (spec: "do this first")
# --------------------------------------------------------------------------- #
def filter_junk(df: pd.DataFrame) -> pd.DataFrame:
    """Drop any row with a null Posting Date or Amount.

    This single rule removes both junk classes the spec names: all-comma
    padding rows (every field empty) and orphan rows carrying only a check
    number. We never treat len(df) as a transaction count before this runs.
    """
    keep = df["Posting Date"].notna() & df["Amount"].notna()
    return df[keep].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 3. Transaction date
# --------------------------------------------------------------------------- #
# The only reason the ledger layer reads the description at all is to recover the
# real transaction date, which can differ from the posting date. We do NOT parse
# a merchant here -- that is the enrichment layer's job.
_TRAILING_DATE = re.compile(r"(\d{1,2})/(\d{1,2})\s*$")


def _infer_txn_date(mm: int, dd: int, posted: date) -> date:
    """Turn a trailing MM/DD (no year) into a real date using posted_date.

    A card purchase always occurs on or a few days before it posts, so the
    transaction year equals the posting year -- UNLESS that would place the
    transaction *after* it posted. That only happens across the Dec->Jan
    rollover (posts 1/3/26, purchased 12/29/25), so in that case step the year
    back by one.
    """
    try:
        candidate = date(posted.year, mm, dd)
    except ValueError:
        # e.g. a bogus 2/29 in a non-leap year; fall back rather than crash.
        return posted
    if candidate > posted:
        try:
            return date(posted.year - 1, mm, dd)
        except ValueError:
            return posted
    return candidate


def extract_txn_date(txn_type: str, desc: str, posted: date) -> date:
    """Recover the transaction date.

    Only DEBIT_CARD rows embed one (a trailing MM/DD, e.g. the '07/27' in
    'Whole Foods RSQ 10103 866-216-1072 DE  07/27'). Every other Type has no
    embedded date, so it falls back to posted_date.
    """
    if txn_type == "DEBIT_CARD":
        m = _TRAILING_DATE.search(desc)
        if m:
            return _infer_txn_date(int(m.group(1)), int(m.group(2)), posted)
    return posted


# --------------------------------------------------------------------------- #
# 4. txn_id
# --------------------------------------------------------------------------- #
def make_txn_id(txn_date: date, amount: float, description: str, account: str) -> str:
    """Content hash used as the primary key.

    Computed from BANK FACTS ONLY -- sha1(date|amount|description|account) --
    never from anything the user edits, so enrichment keyed on txn_id survives
    edits and re-imports. Chase checking exports carry no stable bank-issued id.

    DECISION (not an accident): two byte-identical transactions (same day,
    amount, description, account) collide and one is lost on dedup. Accepted in
    exchange for safe, idempotent re-uploads of overlapping date ranges.
    """
    key = f"{txn_date.isoformat()}|{amount:.2f}|{description}|{account}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 5. Normalize one file
# --------------------------------------------------------------------------- #
def normalize_file(path: str, account: str) -> pd.DataFrame:
    """Read one Chase export and return the fixed ledger schema.

    Order matters: we validate the balance chain while Balance is still in
    hand, then drop it, because a running balance is only meaningful in
    original file order and breaks the moment rows are sorted or deduped.
    """
    source_file = path.rsplit("/", 1)[-1]
    filtered = filter_junk(read_chase_csv(path))

    # Assertion 1: something survived the filter.
    assert len(filtered) > 0, "No transactions left after dropping junk rows"

    posted_date = pd.to_datetime(filtered["Posting Date"], format="%m/%d/%y").dt.date
    amount = filtered["Amount"].astype(float)

    # pending == Balance was blank (NaN or the literal single space). Reduce to
    # a plain numpy bool array so it behaves predictably as a mask below.
    pending = filtered["Balance"].fillna("").astype(str).str.strip().eq("").to_numpy()

    # Assertion 3 (strongest integrity check): on POSTED rows, in original file
    # order, balance[i] == balance[i+1] + amount[i]. Do this before dropping
    # Balance below.
    _check_balance_chain(filtered.loc[~pending], amount.loc[~pending])

    # The only thing we read from the description is the transaction date.
    descriptions = filtered["Description"].astype(str)
    txn_dates = [
        extract_txn_date(str(typ), desc, pdt)
        for typ, desc, pdt in zip(filtered["Type"], descriptions, posted_date)
    ]

    out = pd.DataFrame(
        {
            "date": txn_dates,
            "posted_date": list(posted_date),
            "amount": amount.to_numpy(),
            "description": descriptions.to_numpy(),
            "account": account,
            "pending": pending,
            "source_file": source_file,
        }
    )
    out["txn_id"] = [
        make_txn_id(d, a, desc, account)
        for d, a, desc in zip(out["date"], out["amount"], out["description"])
    ]
    out = out[OUTPUT_COLUMNS]  # enforce column order / membership

    # Dedup within the file (see make_txn_id for the collision tradeoff).
    out = out.drop_duplicates(subset="txn_id", keep="last").reset_index(drop=True)

    # Assertion 2: no null date or amount survived.
    assert out["date"].notna().all(), "Null date in output"
    assert out["amount"].notna().all(), "Null amount in output"
    # Assertion 4: every row has a non-empty description.
    assert (out["description"].str.len() > 0).all(), "Empty description in output"

    return out


def _check_balance_chain(posted_rows: pd.DataFrame, posted_amounts: pd.Series) -> None:
    """balance[i] == balance[i+1] + amount[i] for consecutive posted rows."""
    bal = pd.to_numeric(posted_rows["Balance"], errors="coerce").to_numpy()
    amt = posted_amounts.to_numpy()
    for i in range(len(bal) - 1):
        expected = bal[i + 1] + amt[i]
        assert abs(bal[i] - expected) < 0.005, (
            f"Balance chain broken at posted row {i}: "
            f"{bal[i]:.2f} != {bal[i + 1]:.2f} + {amt[i]:.2f}"
        )


# --------------------------------------------------------------------------- #
# 6. Upsert (safe re-upload of overlapping ranges)
# --------------------------------------------------------------------------- #
def upsert(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Merge new rows into existing, keying on txn_id; new wins on collision."""
    combined = pd.concat([existing, new], ignore_index=True)
    return combined.drop_duplicates(subset="txn_id", keep="last").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 7. Transfers (cross-account; runs on the combined table, not one file)
# --------------------------------------------------------------------------- #
def flag_transfers(df: pd.DataFrame, day_window: int = 3) -> pd.DataFrame:
    """Return a copy with an `is_transfer` bool column.

    Heuristic from the spec: equal absolute amounts, opposite signs, different
    account, dates within a few days. We FLAG matched rows rather than delete
    them, so nothing is silently lost; spend_total() then excludes them.

    `is_transfer` is an analysis-time annotation, deliberately NOT part of the
    fixed OUTPUT_COLUMNS schema.
    """
    out = df.copy()
    out["is_transfer"] = False
    dates = pd.to_datetime(out["date"])

    for i in range(len(out)):
        if out.iloc[i]["is_transfer"]:
            continue
        ai = out.iloc[i]["amount"]
        for j in range(i + 1, len(out)):
            if out.iloc[j]["is_transfer"]:
                continue
            aj = out.iloc[j]["amount"]
            same_magnitude = abs(ai + aj) < 0.005      # opposite signs, equal size
            diff_account = out.iloc[i]["account"] != out.iloc[j]["account"]
            close_in_time = abs((dates.iloc[i] - dates.iloc[j]).days) <= day_window
            if ai != 0 and same_magnitude and diff_account and close_in_time:
                out.iat[i, out.columns.get_loc("is_transfer")] = True
                out.iat[j, out.columns.get_loc("is_transfer")] = True
                break
    return out


def spend_total(df: pd.DataFrame) -> float:
    """Sum of money-out, excluding transfers. Assumes flag_transfers() has run."""
    frame = df if "is_transfer" in df.columns else flag_transfers(df)
    spend = frame[(~frame["is_transfer"]) & (frame["amount"] < 0)]
    return float(spend["amount"].sum())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize a Chase checking CSV export.")
    parser.add_argument("csv", help="path to the Chase export")
    parser.add_argument("--account", required=True, help="account name this file came from")
    args = parser.parse_args()

    df = normalize_file(args.csv, args.account)
    print(df.to_string(index=False))
    print(f"\n{len(df)} transactions | spend total (excl. transfers): {spend_total(df):.2f}")


if __name__ == "__main__":
    main()
