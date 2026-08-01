# Transaction Normalizer Spec (pass one)

Purpose: turn any Chase CSV export into one consistent internal table.
Nothing in this pass touches UI, auth, or hosting.

## Input format observed (Chase checking export)

Header, exactly as Chase writes it:

```
Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
```

- Line endings are CRLF.
- `Details`: `DEBIT` / `CREDIT`.
- `Posting Date`: `M/D/YY` (e.g. `7/29/26`). Parse with an explicit
  `format="%m/%d/%y"`. Do not let pandas infer.
- `Amount`: already signed, negative for money out. Do not derive sign
  from `Details`.
- `Type`: `DEBIT_CARD`, `MISC_DEBIT`, `ACH_DEBIT`, and others not yet seen.
- `Balance`: running balance, present only on posted rows. A literal
  single space `" "` on pending rows, which means pandas reads the whole
  column as `str`, not `float`.
- `Check or Slip #`: usually empty.

## Row filter (do this first)

The export can contain large numbers of junk lines:

- Padding rows that are all commas: `,,,,,,`
- Orphan rows carrying only a check number and nothing else

Rule: **drop any row where `Posting Date` or `Amount` is null.** That
removes both classes. Never trust `len(df)` as a transaction count
before this filter runs.

## Output schema

| column       | type    | notes                                                  |
|--------------|---------|--------------------------------------------------------|
| `txn_id`     | str     | sha1 of `date` + `amount` + `merchant` + `account`      |
| `date`       | date    | transaction date, not posting date (see below)          |
| `posted_date`| date    | from `Posting Date`                                     |
| `amount`     | float   | signed, negative = money out                            |
| `merchant`   | str     | cleaned, human readable                                 |
| `description`| str     | original string, untouched, kept for debugging          |
| `account`    | str     | which account the file came from                        |
| `category`   | str     | nullable in pass one                                    |
| `pending`    | bool    | true when `Balance` was blank                           |
| `source_file`| str     | filename of the export                                  |

Deliberately **not** stored: `Balance`. A running balance is only
meaningful in original file order, so it breaks the moment rows are
sorted, merged, or deduped. Use it during parsing, then drop it.

## Description parsing

There are at least three different formats in one file. Branch on `Type`.

1. **`DEBIT_CARD`** (posted card purchases)
   `Whole Foods RSQ 10103 866-216-1072 DE          07/27`
   Merchant name at the front, and the **real transaction date** at the
   end, which differs from `Posting Date`. Extract that trailing `MM/DD`
   into `date` and infer the year from `posted_date` (watch the December
   to January rollover).

2. **`MISC_DEBIT`** (pending point of sale)
   `POS DEBIT                SAFEWAY #1551            SEATTLE       WA                    2645`
   Fixed-width padded. Strip the `POS DEBIT` prefix, collapse runs of
   whitespace, drop the trailing 4-digit terminal code, then peel off
   the trailing state and city. No transaction date available, so
   `date` falls back to `posted_date`.

3. **`ACH_DEBIT`** (bill pay and direct debits)
   `ORIG CO NAME:SEATTLEUTILTIES  CO ENTRY DESCR:WEB_PAY    SEC:WEB IND ID:... ORIG ID:...`
   Key-value soup. The only useful field is `ORIG CO NAME`. Extract it
   and discard the rest. Note the vendor's own typo in the sample, so do
   not assume company names are spelled correctly.

Unknown `Type` values must not silently fall through. Log them and pass
the collapsed-whitespace description through as `merchant`.

## Merchant cleaning

Two cleanup steps sit on top of the per-`Type` parsers above. Resolution
order for the final `merchant` is: **alias override first, then the parser
output with the order-id split applied.** The parser still runs in every
case, because it is the only source of the transaction `date`.

1. **Order-id split (`MERCHANT*ORDERID`).** Payment aggregators append a
   per-transaction order id after a `*`, e.g. `Audible*2M0FW7SD3`,
   and similar forms from Square, PayPal, and Stripe. Split on the first
   `*` and keep the left side. Everything after the `*` (order id plus any
   trailing location/routing junk) is dropped. A description with no `*` is
   left unchanged.

2. **Alias file (`merchant_aliases.csv`).** A two-column
   `raw,display` map, consulted **before** the parser's cleaning is trusted:
   if any `raw` needle appears (case-insensitive, whitespace-collapsed) in
   the original description, its `display` value becomes the merchant and
   wins over the parser. This is the maintenance escape hatch — a future bad
   name is fixed by **adding a row to the CSV, not editing regex**. A single
   row (`Audible` -> `Audible`) covers every Audible transaction regardless
   of its varying order id, location, or date. Seeded with the Audible row.
   The file is config, not data, so it is committed via a `.gitignore`
   exception even though `*.csv` is ignored.

## Dedup

`txn_id` is a content hash, not a bank-issued ID, because Chase checking
exports do not provide a stable one. Two genuinely separate identical
purchases on the same day at the same merchant will collide and one will
be lost. That is the accepted tradeoff. Make it a documented decision in
code comments, not an accident.

On import, upsert on `txn_id` so overlapping date ranges are safe to
re-upload.

## Transfers

Money moving between accounts appears twice, once negative and once
positive, and will inflate any spending total. Detection heuristic:
equal absolute amounts, opposite signs, different `account`, dates
within a few days. Flag rather than delete, and exclude flagged rows
from spend totals.

## Validation assertions

Run these on every import and fail loudly:

1. Row count after filtering is greater than zero.
2. No null `date` or `amount` survives the filter.
3. On posted rows in original file order, the balance chain reconciles:
   `balance[i] == balance[i+1] + amount[i]`. Verified against the sample.
   This is the strongest integrity check available, so use it while
   `Balance` is still in hand.
4. Every output row has a non-empty `merchant`.

## Known gaps to fill before pass two

- No `CREDIT` rows in the sample, so deposit and refund handling is
  untested. Get an export containing a paycheck or a Zelle receipt.
- Credit card exports use a different schema than checking, including a
  category column Chase supplies itself. Handle separately.
- Amounts appear unformatted in places (`-1` rather than `-1.00`).
  Harmless for float parsing, relevant for display.

## Repo hygiene

`.gitignore` must contain `*.csv`, `data/`, and `.streamlit/secrets.toml`
before the first commit.
