# Demo data

UNIWORK seeds its own SQLite database on first run with synthetic suppliers, SKUs, purchase orders, goods receipts, stock movements and vendor invoices. The records are fictional; email addresses use the reserved `.example` domain. Some invoices intentionally contain discrepancies so the review workflow is visible immediately.

The seed runs only when the vendor table is empty. Delete `instance/uniwork.sqlite3` and restart the app to restore the original demo dataset.
