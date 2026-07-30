"""Chase checking CSV -> one consistent internal transaction table.

Pass one. Implements SPEC-normalizer.md and nothing else: no UI, no auth, no
hosting. Everything here is about turning a raw Chase export into the fixed
output schema, validating it, and providing the dedup / transfer helpers the
spec calls for.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
from datetime import date

import pandas as pd

log = logging.getLogger("normalizer")

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

# The fixed output schema, in order. normalize_file() guarantees exactly these
# columns and nothing else. (Balance is deliberately NOT here -- see below.)
OUTPUT_COLUMNS = [
    "txn_id",
    "date",
    "posted_date",
    "amount",
    "merchant",
    "description",
    "account",
    "category",
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
# 3. Description parsing -- branch on Type
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")


def _collapse(s: str) -> str:
    """Collapse any run of whitespace to a single space and trim the ends."""
    return _WS.sub(" ", str(s)).strip()


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


def _parse_debit_card(desc: str, posted: date) -> tuple[str, date]:
    """DEBIT_CARD: merchant at the front, real MM/DD transaction date at end.

    Example:
      'Whole Foods RSQ 10103 866-216-1072 DE          07/27'
    """
    trailing = re.search(r"(\d{1,2})/(\d{1,2})\s*$", desc)
    if trailing:
        txn_date = _infer_txn_date(int(trailing.group(1)), int(trailing.group(2)), posted)
        head = desc[: trailing.start()]
    else:
        # No trailing date on this card row; fall back to posted_date.
        txn_date = posted
        head = desc

    # Merchant is the leading run of word-ish tokens. Stop at the first token
    # that is a store number / phone number (i.e. has no letters), which is
    # where the trailing location/routing junk begins.
    merchant_tokens: list[str] = []
    for tok in _collapse(head).split():
        if re.search(r"[A-Za-z]", tok):
            merchant_tokens.append(tok)
        else:
            break
    merchant = " ".join(merchant_tokens) or _collapse(head)
    return merchant, txn_date


def _parse_misc_debit(desc: str, posted: date) -> tuple[str, date]:
    """MISC_DEBIT: fixed-width, padded POS line. No transaction date available.

    Example:
      'POS DEBIT                SAFEWAY #1551            SEATTLE       WA          2645'
    -> strip 'POS DEBIT', collapse whitespace, drop trailing 4-digit terminal
       code, then peel the trailing state and city.
    """
    body = _collapse(re.sub(r"^\s*POS DEBIT", "", desc, count=1))
    tokens = body.split()

    if tokens and re.fullmatch(r"\d{4}", tokens[-1]):
        tokens.pop()                                   # terminal code
    if tokens and re.fullmatch(r"[A-Z]{2}", tokens[-1]):
        tokens.pop()                                   # 2-letter state
        if tokens:
            tokens.pop()  # city. NOTE: single token only -- 'SAN FRANCISCO'
                          # would leave 'SAN' behind. Good enough for pass one.
    merchant = " ".join(tokens) or body
    return merchant, posted  # date falls back to posted_date


def _parse_ach_debit(desc: str, posted: date) -> tuple[str, date]:
    """ACH_DEBIT: key-value soup; the only useful field is ORIG CO NAME.

    Example:
      'ORIG CO NAME:SEATTLEUTILTIES  CO ENTRY DESCR:WEB_PAY  SEC:WEB IND ID:...'
    The value ends at the next run of 2+ spaces (the padding before the next
    key) or at end of string. We do NOT correct vendor typos -- 'SEATTLEUTILTIES'
    is passed through exactly as the bank wrote it.
    """
    m = re.search(r"ORIG CO NAME:\s*(.+?)(?:\s{2,}|$)", desc)
    merchant = _collapse(m.group(1)) if m else _collapse(desc)
    return merchant, posted  # date falls back to posted_date


# Dispatch table. Every branch returns (merchant, transaction_date).
_PARSERS = {
    "DEBIT_CARD": _parse_debit_card,
    "MISC_DEBIT": _parse_misc_debit,
    "ACH_DEBIT": _parse_ach_debit,
}


def parse_description(txn_type: str, desc: str, posted: date) -> tuple[str, date]:
    """Route to the right parser by Type; never let an unknown Type fall through."""
    parser = _PARSERS.get(txn_type)
    if parser is not None:
        return parser(desc, posted)
    # Unknown Type: log it (so new formats surface) and pass the cleaned
    # description straight through as the merchant, date falls back to posted.
    log.warning("Unknown Type %r; passing description through: %r", txn_type, desc)
    return _collapse(desc), posted


# --------------------------------------------------------------------------- #
# 4. txn_id
# --------------------------------------------------------------------------- #
def make_txn_id(txn_date: date, amount: float, merchant: str, account: str) -> str:
    """Content hash used as the primary key.

    DECISION (not an accident): Chase checking exports carry no stable
    bank-issued id, so txn_id is sha1(date|amount|merchant|account). The known
    tradeoff is that two genuinely separate but identical purchases on the same
    day, same merchant, same account collide and one is lost on dedup. We
    accept that in exchange for safe re-uploads of overlapping date ranges.
    """
    key = f"{txn_date.isoformat()}|{amount:.2f}|{merchant}|{account}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 5. Normalize one file
# --------------------------------------------------------------------------- #
def normalize_file(path: str, account: str) -> pd.DataFrame:
    """Read one Chase export and return the fixed output schema.

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

    # Parse each row's description by Type into (merchant, transaction date).
    merchants: list[str] = []
    txn_dates: list[date] = []
    for typ, desc, pdt in zip(filtered["Type"], filtered["Description"], posted_date):
        merch, tdate = parse_description(str(typ), str(desc), pdt)
        merchants.append(merch)
        txn_dates.append(tdate)

    out = pd.DataFrame(
        {
            "date": txn_dates,
            "posted_date": list(posted_date),
            "amount": amount.to_numpy(),
            "merchant": merchants,
            "description": filtered["Description"].astype(str).to_numpy(),
            "account": account,
            "category": None,               # nullable in pass one
            "pending": pending,
            "source_file": source_file,
        }
    )
    out["txn_id"] = [
        make_txn_id(d, a, m, account)
        for d, a, m in zip(out["date"], out["amount"], out["merchant"])
    ]
    out = out[OUTPUT_COLUMNS]  # enforce column order / membership

    # Dedup within the file (see make_txn_id for the collision tradeoff).
    out = out.drop_duplicates(subset="txn_id", keep="last").reset_index(drop=True)

    # Assertion 2: no null date or amount survived.
    assert out["date"].notna().all(), "Null date in output"
    assert out["amount"].notna().all(), "Null amount in output"
    # Assertion 4: every row has a non-empty merchant.
    assert (out["merchant"].str.len() > 0).all(), "Empty merchant in output"

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

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df = normalize_file(args.csv, args.account)
    print(df.to_string(index=False))
    print(f"\n{len(df)} transactions | spend total (excl. transfers): {spend_total(df):.2f}")


if __name__ == "__main__":
    main()
