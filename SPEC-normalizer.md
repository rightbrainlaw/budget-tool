# Transaction Normalizer Spec (pass one — ledger layer)

Purpose: turn any Chase CSV export into one consistent **ledger** — the plain
facts the bank gives us, and nothing interpretive. Meaning (which merchant a
row represents, what budget category it belongs to) is deliberately **not**
this layer's job; it belongs to the enrichment layer described at the end.
Nothing in this pass touches UI, auth, or hosting.

## Two layers, and why they are separate

- **Ledger layer (this spec).** Derived purely from the bank file: date,
  amount, raw description, account, etc. Immutable — re-importing the same
  transaction always reproduces the same row, including the same `txn_id`.
  The normalizer never guesses at what a description "means."
- **Enrichment layer (pass two, sketched below).** User-assigned `merchant`
  and `category`, stored separately and joined back to the ledger by `txn_id`.

The split exists so that regex on messy bank memos never becomes load-bearing.
Earlier drafts of this spec tried to parse a clean `merchant` out of the
description by branching on `Type`; that was the most fragile part of the
pipeline and is removed. Merchant is now curated data, not a parse result.

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
- `Type`: `DEBIT_CARD`, `MISC_DEBIT`, `ACH_DEBIT`, `ACH_CREDIT`, `ACCT_XFER`,
  and others not yet seen. Note that `Type` does **not** map cleanly to
  description format (e.g. some `MISC_DEBIT` rows are ACH-style, not POS
  lines), which is one more reason we no longer parse meaning out of it.
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

## Output schema (ledger only)

| column       | type    | notes                                                   |
|--------------|---------|---------------------------------------------------------|
| `txn_id`     | str     | sha1 of `date` + `amount` + `description` + `account`   |
| `date`       | date    | transaction date, not posting date (see below)          |
| `posted_date`| date    | from `Posting Date`                                     |
| `amount`     | float   | signed, negative = money out                            |
| `description`| str     | original bank string, untouched                         |
| `account`    | str     | which account the file came from                        |
| `pending`    | bool    | true when `Balance` was blank                           |
| `source_file`| str     | filename of the export                                  |

Deliberately **not** in this layer:

- `merchant` and `category` — enrichment, assigned by the user later and
  joined by `txn_id`. See the enrichment section.
- `Balance` — a running balance is only meaningful in original file order, so
  it breaks the moment rows are sorted, merged, or deduped. Use it during
  validation (below), then drop it.

## Transaction date

The only reason the ledger layer reads the description at all is to recover the
real transaction date, which can differ from the posting date.

- **`DEBIT_CARD`** rows carry a trailing `MM/DD`, e.g.
  `Whole Foods RSQ 10103 866-216-1072 DE          07/27`. Extract it into
  `date` and infer the year from `posted_date`, handling the December→January
  rollover: a purchase posts on or a few days after it occurs, so if using the
  posting year would place the transaction *after* it posted, step the year
  back by one.
- **All other `Type`s** have no embedded transaction date, so `date` falls
  back to `posted_date`.

No merchant extraction, no `Type`-branching beyond "is there a trailing date."
The raw `description` is stored exactly as the bank wrote it, typos and all.

## txn_id and dedup

`txn_id` is a content hash, not a bank-issued ID, because Chase checking
exports do not provide a stable one. It is computed from **bank facts only**
(`date` + `amount` + `description` + `account`) — never from anything a user
edits. Two consequences make this the linchpin of the whole design:

1. Assigning or changing a `merchant`/`category` never changes `txn_id`, so
   enrichment stays attached to its row.
2. Re-importing an overlapping date range reproduces the identical `txn_id`,
   so **enrichment survives re-imports for free.**

Tradeoff, to be documented in code, not discovered by accident: two genuinely
separate but byte-identical transactions (same day, amount, description,
account) collide and one is lost on dedup. Accepted in exchange for safe,
idempotent re-uploads. On import, upsert on `txn_id`.

## Transfers

Money moving between accounts appears twice, once negative and once
positive, and will inflate any spending total. Detection heuristic:
equal absolute amounts, opposite signs, different `account`, dates
within a few days. Flag rather than delete, and exclude flagged rows
from spend totals. (A single-file import cannot pair the two legs; this
runs on the combined multi-account ledger.)

## Validation assertions

Run these on every import and fail loudly:

1. Row count after filtering is greater than zero.
2. No null `date` or `amount` survives the filter.
3. On posted rows in original file order, the balance chain reconciles:
   `balance[i] == balance[i+1] + amount[i]`. Verified against the sample.
   This is the strongest integrity check available, so use it while
   `Balance` is still in hand.
4. Every output row has a non-empty `description`.

## Enrichment layer (pass two — design, not yet built)

Meaning is added on top of the immutable ledger, keyed by `txn_id`. This is how
the mainstream apps work and how Dan has built it before in Sheets.

- **`merchant` is a saveable entity, not a parsed string.** A "Merchants" list
  (`merchant_id`, `name`, optional `default_category`) backs a dropdown. You
  look at a transaction, decide it's Amazon, pick or add Amazon, and save. This
  preserves "you spent $200 at Amazon this month" without any name being
  auto-identified from the memo.
- **`category` is the required reporting field; `merchant` is optional.** Rows
  with no merchant still report by category.
- **Remembered mappings (optional, additive).** Categorizing one transaction
  can create a `description-signature → merchant/category` mapping so future
  matching rows auto-fill. This is memory generated *from your choices*, not
  hand-authored regex. With it off, you just pick manually more often. (This
  supersedes the old `merchant_aliases.csv` idea, which was authored regex
  aimed at a cosmetic name.)
- **Completeness gate (soft).** Before running reports for a period, surface
  any transactions still missing a category ("12 need review") and warn, rather
  than hard-blocking access to your own data.

Sketch:

```
Transactions (ledger, immutable)        Enrichment (user-assigned)
  txn_id  ← PK, hash of bank facts  ──┐   txn_id       ← FK
  date, posted_date, amount           └─▶ merchant_id  ← FK, nullable
  description (raw)                        category     ← required for reports
  account, pending, source_file           reviewed?

Merchants (saveable)          Remembered mappings (optional, auto-grown)
  merchant_id, name             match_pattern → merchant_id / category
  default_category?

Reports = Transactions ⋈ Enrichment, gated on category completeness
```

## Known gaps to fill before pass two

- `CREDIT` rows (deposits, refunds, payroll) now appear in real exports and
  arrive as `Type=ACH_CREDIT` and similar. They parse into the ledger fine
  (positive `amount`), but income vs. spend handling — likely a `kind`
  dimension or a set of categories (Income, Transfer) — is undesigned.
- Credit card exports use a different schema than checking, including a
  category column Chase supplies itself. Handle separately; the header check
  rejects them today.
- Amounts appear unformatted in places (`-1` rather than `-1.00`).
  Harmless for float parsing, relevant for display.

## Repo hygiene

`.gitignore` contains `*.csv`, `data/`, and `.streamlit/secrets.toml`. The
invented test fixture (`tests/sample_transactions.csv`) is committed via an
explicit `!` exception so real exports stay ignored while the fixture is
tracked.
