# UNIWORK — Procurement & Inventory under GST

This updated demo is split into a clean **frontend** and **backend** while retaining the existing light-green/black UI.

## Folder structure

```text
UNIWORK/
├── frontend/
│   ├── templates/          # Jinja pages / UI
│   └── static/             # CSS, JavaScript, assets
├── backend/
│   ├── app.py              # Flask routes, session auth, business logic
│   ├── run.py              # local server entry point
│   ├── requirements.txt
│   ├── .env.example
│   ├── data/                # demo scenario and data notes
│   ├── instance/            # local SQLite database (created at runtime)
│   └── tests/               # backend tests
└── README.md
```

## Run on Windows PowerShell

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:5000`. The local demo creates an administrator on first launch. Defaults are `admin` / `Forge@2026!`; change the password immediately from the Password link. Set `ADMIN_USERNAME`, `ADMIN_PASSWORD` and a strong `SECRET_KEY` in the environment before any shared deployment. This is a local demonstration, not a production deployment configuration.


## Frontend ↔ backend connection

This version uses **Flask with server-rendered Jinja templates**. The frontend is already connected to the backend; it does not need a separate frontend server. The connection is configured in `backend/app.py`:

```python
app = Flask(
    __name__,
    template_folder="../frontend/templates",
    static_folder="../frontend/static",
    static_url_path="/static",
)
```

- Pages in `frontend/templates/` are rendered by Flask routes in `backend/app.py` using `render_template(...)`.
- CSS and JavaScript in `frontend/static/` are served at `/static/`; templates load them with `url_for('static', filename='app.css')` and `url_for('static', filename='app.js')`.
- Forms send requests to backend routes using `action="{{ url_for('route_name') }}"`.
- Database reads/writes and permission checks remain in the backend. Do not open the HTML files directly from File Explorer; start Flask and browse to `http://127.0.0.1:5000`.

### Verify the connection

1. Start the backend using the commands above.
2. Open `http://127.0.0.1:5000` and sign in.
3. Open Vendors, add a demo vendor, and confirm it appears in the vendor list.
4. Open the avatar/Profile page. The **Log out** button is available beside Change password.
5. Click Log out; the app submits a POST request to `/logout`, clears the session, and returns to the sign-in page. Sign in again to confirm the session flow.

If you later want a separately deployed React/Vue frontend, the backend will need a JSON REST API, CORS configuration, and a frontend API base URL. That is a different architecture from this currently working Flask/Jinja version.

## Implemented in this update

- Split frontend templates/static assets from backend Python and tests.
- Vendor master create form (admin/procurement roles), required-field checks, duplicate vendor-code handling, GSTIN uppercase normalization, and audit log entry.
- Vendor directory search across vendor name, code, GSTIN, state and email.
- Working global page search for visible tables and cards; Escape clears the search.
- Profile page opened by clicking the top-right avatar, with role/status/account facts and recent audit activity.
- Existing UI theme and layouts preserved; only supporting controls and vendor/profile screens were added.
- Existing procurement, goods receipt, inventory, invoice/GST, reporting, user-role, audit and data-management workflows remain in the application.

## Scope and assumptions

- GST computation and ITC checks are simplified demo rules. Validate against the current statutory schema and your GST professional before real-world use. GST portal filing is simulated and not performed.
- The database and seeded data are synthetic. Vendor master data is not encrypted at rest; do not put real confidential vendor bank details into this demo.
- Financial activity is appended to an audit table, but SQLite file/database administrators can still alter it. This is not cryptographically tamper-proof audit storage.
- E-invoice/e-way bill live integrations, production planning, payroll and fixed assets are out of scope.

## Tests

From `backend/`, run:

```powershell
python -m unittest discover -s tests -v
```


## Independent logins in separate tabs

UNIWORK uses server-side, tab-specific authentication tokens rather than relying on one shared login identity in the browser cookie. Open the login page separately in each new tab and sign in with the account for that tab. Same-tab navigation retains its token, and logging out revokes only that tab's token. Links opened with `target="_blank"` deliberately start a fresh login flow.

**Security note:** the `__tab` token is present in the page URL so normal HTML navigation can retain the correct tab identity. Treat authenticated URLs as sensitive and do not share them. For production deployment, use a dedicated frontend/API architecture with stronger token transport and CSRF protections, HTTPS, secure secret configuration, and session expiry/revocation controls.

## Latest vendor and invoice workflow updates

- Vendor cards are clickable and open a supplier detail page with master data, linked purchase orders, line-item quantities/prices, estimated GST-inclusive totals, and linked vendor invoices.
- Invoice validation checks both PO taxable-value variance and the configured demo GST calculation before auto-approval.
- A matching pending invoice can be approved through the normal review action. An invoice with exceptions stays pending when rechecked; an authorized Administrator or Accounts user can use the explicit Override action only after entering a reason. Override actions are written to the audit trail, and unresolved exceptions keep ITC marked Review.
- The supplied geometric-cubes image is used as a subtle, darkened page background at `frontend/static/cubes-bg.jpeg` so content remains readable.
