import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDAT\x08\x99c``\x00\x00\x00\x04\x00\x01\xf6\x178U"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def load_panel_module(temp_root, panel_username="", panel_password=""):
    data_dir = temp_root / "data"
    xray_dir = temp_root / "xray"
    runtime_dir = xray_dir / "runtime"
    logs_dir = xray_dir / "logs"
    reports_dir = xray_dir / "reports" / "hourly-domains"
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    client_config_path = temp_root / "client-test.json"
    client_config_path.write_text(
        json.dumps(
            {
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {
                            "vnext": [
                                {
                                    "address": "example.com",
                                    "port": 443,
                                    "users": [
                                        {
                                            "id": "11111111-1111-1111-1111-111111111111",
                                            "flow": "xtls-rprx-vision",
                                        }
                                    ],
                                }
                            ]
                        },
                        "streamSettings": {
                            "realitySettings": {
                                "serverName": "www.example.com",
                                "publicKey": "pubkey-example",
                                "shortId": "0123456789abcdef",
                                "fingerprint": "chrome",
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    env_file_path = xray_dir / ".env"
    env_file_path.write_text(
        "\n".join(
            [
                "XRAY_LISTEN_HOST=0.0.0.0",
                "XRAY_LISTEN_PORT=443",
                "XRAY_PUBLIC_HOST=panel.example.com",
                "XRAY_CLIENT_UUID=11111111-1111-1111-1111-111111111111",
                "XRAY_FLOW=xtls-rprx-vision",
                "XRAY_REALITY_PRIVATE_KEY=private-key-example",
                "XRAY_REALITY_PUBLIC_KEY=public-key-example",
                "XRAY_REALITY_SHORT_ID=0123456789abcdef",
                "XRAY_SERVER_NAME=www.example.com",
                "XRAY_DEST=www.example.com:443",
                "XRAY_FINGERPRINT=chrome",
                "XRAY_LOGLEVEL=warning",
                "XRAY_NODE_TAG=test-node",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DB_PATH"] = str(data_dir / "panel.db")
    os.environ["XRAY_ENV_FILE_PATH"] = str(env_file_path)
    os.environ["XRAY_CONFIG_PATH"] = str(runtime_dir / "config.json")
    os.environ["XRAY_PANEL_PORTS_PATH"] = str(runtime_dir / "panel-ports.json")
    os.environ["XRAY_ACCESS_LOG_PATH"] = str(logs_dir / "access.log")
    os.environ["DATAPLANE_CONTAINER_NAME"] = "test-xray-container"
    os.environ["XRAY_CLIENT_CONFIG_PATH"] = str(client_config_path)
    os.environ["PANEL_PUBLIC_URL"] = "http://panel.example.com"
    os.environ["SEED_LISTEN_PORT"] = ""
    os.environ["PANEL_USERNAME"] = panel_username
    os.environ["PANEL_PASSWORD"] = panel_password
    os.environ["PANEL_SECRET_KEY"] = "test-secret-key"
    os.environ["PROBE_ENABLED"] = "0"
    os.environ["COMMERCE_AUTO_PORT_START"] = "35000"
    os.environ["COMMERCE_AUTO_PORT_END"] = "35005"

    for module_name in list(sys.modules):
        if module_name.startswith("app."):
            sys.modules.pop(module_name, None)

    module = importlib.import_module("app.panel")
    module = importlib.reload(module)
    module.state.render_xray_config = lambda: None
    module.state.xray_config_test = lambda: None
    module.state.restart_data_plane = lambda: True
    module.state.data_plane_configured = lambda: False
    module.state.data_plane_running = lambda: False
    module.state.init_db()
    return module


class CommerceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.panel = load_panel_module(self.root)
        self.client = self.panel.app.test_client()
        self.plan_payload = self.panel.state.validate_plan_payload(
            {
                "slug": "basic-30d-100g",
                "name": "基础套餐",
                "description": "30 天 100G",
                "price_fen": "990",
                "duration_days": "30",
                "traffic_limit": "100G",
                "enabled": True,
                "sort_order": "1",
            }
        )
        self.panel.state.create_plan(self.plan_payload)

    def tearDown(self):
        self.tempdir.cleanup()

    def csrf_token(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def register_customer(self, email="user@example.com", password="Password123!"):
        self.client.get("/customer/register")
        response = self.client.post(
            "/customer/register",
            data={
                "csrf_token": self.csrf_token(),
                "next": "/customer/dashboard",
                "email": email,
                "password": password,
                "confirm_password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        return email, password

    def login_customer(self, email="user@example.com", password="Password123!"):
        self.client.get("/customer/login")
        response = self.client.post(
            "/customer/login",
            data={
                "csrf_token": self.csrf_token(),
                "next": "/customer/dashboard",
                "email": email,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def create_existing_order(self):
        plan = self.panel.state.get_plan_by_slug("basic-30d-100g")
        order_no = self.panel.state.create_order(1, plan["id"], kind="new_purchase")
        return self.panel.state.get_customer_order(1, order_no)

    def submit_payment_proof(self, order_no, payer_note="支付宝已付款"):
        response = self.client.post(
            f"/customer/orders/{order_no}/payment-proof",
            data={
                "csrf_token": self.csrf_token(),
                "payer_note": payer_note,
                "proof_image": (io.BytesIO(PNG_BYTES), "proof.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def admin_json_headers(self):
        return {"X-CSRF-Token": self.csrf_token(), "Content-Type": "application/json"}

    def test_existing_package_order_can_be_fulfilled(self):
        email, password = self.register_customer()

        dashboard = self.client.get("/customer/dashboard", follow_redirects=True)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(email, dashboard.get_data(as_text=True))

        self.client.get("/customer/logout", follow_redirects=False)
        self.login_customer(email, password)

        order = self.create_existing_order()
        self.assertEqual(order["status"], "pending_payment")

        self.submit_payment_proof(order["order_no"], payer_note="付款人尾号 1234")
        updated_order = self.panel.state.get_customer_order(1, order["order_no"])
        self.assertEqual(updated_order["status"], "payment_submitted")

        response = self.client.post(
            f"/api/orders/{updated_order['id']}/fulfill",
            data=json.dumps({"review_note": "已核对到账"}),
            headers=self.admin_json_headers(),
        )
        self.assertEqual(response.status_code, 200)

        fulfilled_order = self.panel.state.get_customer_order(1, order["order_no"])
        self.assertEqual(fulfilled_order["status"], "fulfilled")

        services = self.panel.state.query_customer_service_subscriptions(1)
        self.assertEqual(len(services), 1)
        service = services[0]
        self.assertEqual(service["listen_port"], 35000)
        self.assertEqual(service["status"], "active")
        self.assertTrue(service["tenant_token"])
        self.assertTrue(service["subscription_token"])

    def test_package_preorder_routes_are_removed(self):
        self.register_customer()

        plans_page = self.client.get("/plans")
        self.assertEqual(plans_page.status_code, 200)
        self.assertNotIn("立即下单", plans_page.get_data(as_text=True))

        self.assertEqual(self.client.get("/checkout/basic-30d-100g").status_code, 404)
        response = self.client.post(
            "/orders",
            data={"csrf_token": self.csrf_token(), "plan_slug": "basic-30d-100g"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.post(
            "/api/customer/orders",
            data=json.dumps({"plan_slug": "basic-30d-100g"}),
            headers=self.admin_json_headers(),
        )
        self.assertEqual(response.status_code, 405)

    def test_fulfill_order_rolls_back_when_render_fails(self):
        self.register_customer()
        order = self.create_existing_order()
        self.submit_payment_proof(order["order_no"])
        payment_submitted_order = self.panel.state.get_customer_order(1, order["order_no"])

        def fail_render():
            raise RuntimeError("render failed")

        self.panel.state.render_xray_config = fail_render

        response = self.client.post(
            f"/api/orders/{payment_submitted_order['id']}/fulfill",
            data=json.dumps({"review_note": "到账"}),
            headers=self.admin_json_headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("render failed", response.get_data(as_text=True))

        rolled_back_order = self.panel.state.get_customer_order(1, order["order_no"])
        self.assertEqual(rolled_back_order["status"], "payment_submitted")
        self.assertEqual(self.panel.state.query_customer_service_subscriptions(1), [])
        self.assertEqual(self.panel.state.query_ports(), [])

    def test_expired_commercial_service_is_disabled_but_kept_for_renewal(self):
        self.register_customer()
        for _ in range(2):
            order = self.create_existing_order()
            self.submit_payment_proof(order["order_no"])
            self.panel.state.fulfill_order(order["id"])
        services = self.panel.state.query_customer_service_subscriptions(1)
        expired, active = services
        with self.panel.state.connect() as conn:
            conn.execute(
                "UPDATE ports SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", expired["port_id"]),
            )
            conn.commit()
        with mock.patch.object(self.panel.state, "restart_data_plane", return_value=True) as restart:
            self.assertEqual(self.panel.state.disable_auto_stopped_ports(), 1)
            restart.assert_called_once()
            self.assertEqual(self.panel.state.disable_auto_stopped_ports(), 0)
            restart.assert_called_once()
        with self.panel.state.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT enabled FROM ports WHERE id = ?", (expired["port_id"],)).fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT enabled FROM ports WHERE id = ?", (active["port_id"],)).fetchone()[0], 1
            )
            self.assertEqual(self.panel.state.render_panel_ports_payload(conn), {"ports": [active["listen_port"]]})
        renewed = self.panel.state.get_customer_service_subscription(1, expired["id"])
        self.assertTrue(renewed["renewal_allowed"])
        self.assertEqual(renewed["subscription_token"], expired["subscription_token"])

    def test_renewal_requires_quota_or_expired_and_keeps_same_port(self):
        self.register_customer()
        order = self.create_existing_order()
        self.submit_payment_proof(order["order_no"])
        payment_submitted_order = self.panel.state.get_customer_order(1, order["order_no"])
        response = self.client.post(
            f"/api/orders/{payment_submitted_order['id']}/fulfill",
            data=json.dumps({"review_note": "到账"}),
            headers=self.admin_json_headers(),
        )
        self.assertEqual(response.status_code, 200)

        service = self.panel.state.query_customer_service_subscriptions(1)[0]
        with self.assertRaises(self.panel.ValidationError):
            self.panel.state.create_order(
                1,
                service["plan_id"],
                kind="renewal",
                service_subscription_id=service["id"],
            )

        with self.panel.state.connect() as conn:
            conn.execute(
                """
                INSERT INTO traffic_totals (
                    listen_port, total_connections, total_bytes_sent, total_bytes_received, last_seen
                ) VALUES (?, 0, ?, 0, NULL)
                ON CONFLICT(listen_port) DO UPDATE SET total_bytes_sent = excluded.total_bytes_sent
                """,
                (service["listen_port"], service["traffic_limit_bytes"]),
            )
            conn.commit()
        self.panel.state.disable_auto_stopped_ports(reload_xray=False)
        quota_service = self.panel.state.query_customer_service_subscriptions(1)[0]
        self.assertEqual(quota_service["status"], "quota")

        renewal_order_no = self.panel.state.create_order(
            1,
            quota_service["plan_id"],
            kind="renewal",
            service_subscription_id=quota_service["id"],
        )
        self.submit_payment_proof(renewal_order_no)
        renewal_order = self.panel.state.get_customer_order(1, renewal_order_no)
        fulfill_response = self.client.post(
            f"/api/orders/{renewal_order['id']}/fulfill",
            data=json.dumps({"review_note": "续费到账"}),
            headers=self.admin_json_headers(),
        )
        self.assertEqual(fulfill_response.status_code, 200)

        renewed_service = self.panel.state.query_customer_service_subscriptions(1)[0]
        self.assertEqual(renewed_service["listen_port"], service["listen_port"])
        self.assertEqual(renewed_service["subscription_token"], service["subscription_token"])
        self.assertEqual(renewed_service["status"], "active")
        self.assertEqual(renewed_service["traffic_usage_bytes"], 0)

    def test_payment_proof_requires_supported_image(self):
        self.register_customer()
        order = self.create_existing_order()
        response = self.client.post(
            f"/customer/orders/{order['order_no']}/payment-proof",
            data={
                "csrf_token": self.csrf_token(),
                "payer_note": "bad",
                "proof_image": (io.BytesIO(b"not-an-image"), "proof.txt"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        redirected = response.headers["Location"]
        self.assertIn("level=error", redirected)
        updated_order = self.panel.state.get_customer_order(1, order["order_no"])
        self.assertEqual(updated_order["status"], "pending_payment")
