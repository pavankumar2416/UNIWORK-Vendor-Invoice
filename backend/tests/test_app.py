import os
import tempfile
import unittest
from pathlib import Path

os.environ["UNIWORK_DB"] = str(Path(tempfile.gettempdir()) / "uniwork-test-suite.sqlite3")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as forge


class UNIWORKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            Path(os.environ["UNIWORK_DB"]).unlink()
        except FileNotFoundError:
            pass
        forge.app.config["TESTING"] = True
        cls.client = forge.app.test_client()

    def setUp(self):
        response = self.client.get("/login")
        token_url = response.headers.get("Location", "/login")
        from urllib.parse import urlsplit, parse_qs
        token = parse_qs(urlsplit(token_url).query).get("__tab", [""])[0]
        self.client.get(token_url)
        self.client.post("/login?__tab=" + token,
                         data={"username": "admin", "password": "Forge@2026!"},
                         follow_redirects=True)

    def test_login_required(self):
        client = forge.app.test_client()
        response = client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"/login", response.headers["Location"].encode())

    def test_dashboard_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"OPERATIONS SNAPSHOT", response.data)

    def test_inventory_page_loads(self):
        response = self.client.get("/inventory")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SKU register", response.data)

    def test_vendor_directory_and_profile_load(self):
        self.assertEqual(self.client.get("/vendors").status_code, 200)
        response = self.client.get("/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"My profile", response.data)

    def test_create_vendor(self):
        response = self.client.post("/vendors/create", data={
            "code": "VND-TEST-901", "name": "Test Components Pvt Ltd",
            "gstin": "29ABCDE1234F1Z5", "state": "Karnataka",
            "email": "ap@test.example", "phone": "0801234567",
            "payment_terms": "30"
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Components Pvt Ltd", response.data)

    def test_summary_api(self):
        response = self.client.get("/api/summary")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertGreater(data["inventory_skus"], 0)
        self.assertIn("invoice_exceptions", data)

    def test_csv_export(self):
        response = self.client.get("/export/inventory.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"sku,name,category", response.data)

    def test_unknown_export(self):
        response = self.client.get("/export/nope.csv")
        self.assertEqual(response.status_code, 404)

    def test_admin_pages_load(self):
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        self.assertEqual(self.client.get("/admin/data").status_code, 200)

    def test_viewer_cannot_access_admin_page(self):
        db = forge.get_db()
        db.execute("INSERT OR IGNORE INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                   ("testviewer", forge.generate_password_hash("ViewerPass123!"), "Test Viewer", "viewer", "2026-01-01T00:00:00"))
        db.commit()
        viewer = forge.query_one("SELECT id FROM users WHERE username='testviewer'")
        client = forge.app.test_client()
        token = "test-viewer-tab-token"
        now = forge.datetime.now().isoformat(timespec="seconds")
        forge.get_db().execute(
            "INSERT OR REPLACE INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?)",
            (token, viewer["id"], forge.BOOT_ID, now, now))
        forge.get_db().commit()
        self.assertEqual(client.get("/admin/data?__tab=" + token).status_code, 403)


if __name__ == "__main__":
    unittest.main()
