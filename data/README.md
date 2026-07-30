# data/

Drop your **real** Chase CSV exports here.

Everything in this folder is git-ignored (see `.gitignore`) so real financial
data never gets committed. Only this README is tracked, to keep the folder in
the repo and explain its purpose.

Usage:

    python normalizer.py data/your_export.csv --account "chase-checking"
