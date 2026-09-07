from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

from ..config import (
    COMMERCE_AUTO_PORT_END,
    COMMERCE_AUTO_PORT_START,
    COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    LOCAL_TZ,
    PAYMENT_PROOF_MAX_BYTES,
    PAYMENT_PROOFS_DIR,
)
from ..errors import ValidationError
from ..helpers import (
    format_display_time,
    generate_subscription_token,
    generate_tenant_password,
    human_bytes,
    normalize_customer_email,
    parse_data_size,
    status_payload,
    utc_iso_now,
    utc_now,
)
from ..xray.operation_lock import LockBusyError, exclusive_file_lock

from ._constants import PLAN_SLUG_RE


class CommerceService:
    def __init__(self, panel):
        self._panel = panel
    def ensure_commerce_schema(self, conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price_fen INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'CNY',
                duration_days INTEGER NOT NULL,
                traffic_limit_bytes INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL UNIQUE,
                customer_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                service_subscription_id INTEGER,
                status TEXT NOT NULL,
                plan_name_snapshot TEXT NOT NULL,
                plan_description_snapshot TEXT NOT NULL DEFAULT '',
                price_fen_snapshot INTEGER NOT NULL,
                currency_snapshot TEXT NOT NULL DEFAULT 'CNY',
                duration_days_snapshot INTEGER NOT NULL,
                traffic_limit_bytes_snapshot INTEGER NOT NULL,
                expires_at TEXT,
                fulfilled_at TEXT,
                cancelled_at TEXT,
                rejection_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_id) REFERENCES plans(id)
            );

            CREATE TABLE IF NOT EXISTS service_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                port_id INTEGER,
                source_order_id INTEGER NOT NULL,
                latest_order_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_id) REFERENCES plans(id),
                FOREIGN KEY(port_id) REFERENCES ports(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS order_payment_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                proof_image_path TEXT NOT NULL,
                payer_note TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL,
                review_note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                review_status TEXT NOT NULL DEFAULT 'submitted',
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_service_subscription_id ON orders(service_subscription_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_service_subscriptions_customer_id ON service_subscriptions(customer_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_service_subscriptions_port_id ON service_subscriptions(port_id) WHERE port_id IS NOT NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_payment_submissions_order_id ON order_payment_submissions(order_id)"
        )
        self._panel.ensure_commerce_settings_in_tx(conn)
    def ensure_commerce_settings_in_tx(self, conn):
        defaults = {
            "commerce_payment_qr_code_url": "",
            "commerce_payment_instructions": "请扫码支付宝付款后上传截图，并填写付款备注以便人工审核。",
            "commerce_order_expiry_hours": str(COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT),
        }
        for key, value in defaults.items():
            if self._panel.get_state(conn, key, None) is None:
                self._panel.set_state(conn, key, value)
    def get_commerce_settings(self):
        with self._panel.connect() as conn:
            self._panel.ensure_commerce_settings_in_tx(conn)
            return self._panel.serialize_commerce_settings(conn)
    def serialize_commerce_settings(self, conn):
        payment_qr_code_url = str(self._panel.get_state(conn, "commerce_payment_qr_code_url", "") or "").strip()
        payment_instructions = str(
            self._panel.get_state(conn, "commerce_payment_instructions", "") or ""
        ).strip()
        try:
            order_expiry_hours = int(
                str(self._panel.get_state(conn, "commerce_order_expiry_hours", COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT) or COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT)
            )
        except ValueError:
            order_expiry_hours = COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT
        order_expiry_hours = max(order_expiry_hours, 1)
        return {
            "payment_qr_code_url": payment_qr_code_url,
            "payment_instructions": payment_instructions,
            "order_expiry_hours": order_expiry_hours,
            "auto_port_start": COMMERCE_AUTO_PORT_START,
            "auto_port_end": COMMERCE_AUTO_PORT_END,
            "payment_proof_max_bytes": PAYMENT_PROOF_MAX_BYTES,
            "payment_proof_max_display": human_bytes(PAYMENT_PROOF_MAX_BYTES),
        }
    def update_commerce_settings(self, payload):
        instructions = str(payload.get("payment_instructions", "") or "").strip()
        if len(instructions) > 1000:
            raise ValidationError("付款说明不能超过 1000 个字符。")
        payment_qr_code_url = str(payload.get("payment_qr_code_url", "") or "").strip()
        raw_expiry = str(payload.get("order_expiry_hours", "") or "").strip()
        try:
            order_expiry_hours = int(raw_expiry)
        except ValueError as exc:
            raise ValidationError("订单有效期必须是正整数小时。") from exc
        if order_expiry_hours <= 0:
            raise ValidationError("订单有效期必须是正整数小时。")

        def operation(conn):
            self._panel.set_state(conn, "commerce_payment_qr_code_url", payment_qr_code_url)
            self._panel.set_state(conn, "commerce_payment_instructions", instructions)
            self._panel.set_state(conn, "commerce_order_expiry_hours", str(order_expiry_hours))
            return self._panel.serialize_commerce_settings(conn)

        return self._panel.apply_state_update(operation)
    def validate_customer_password(self, password, confirm_password=""):
        raw_password = str(password or "")
        if len(raw_password) < 8:
            raise ValidationError("密码至少需要 8 个字符。")
        if len(raw_password) > 200:
            raise ValidationError("密码不能超过 200 个字符。")
        if confirm_password != "" and raw_password != str(confirm_password or ""):
            raise ValidationError("两次输入的密码不一致。")
        return raw_password
    def slugify_plan_value(self, value):
        slug = PLAN_SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
        if not slug:
            raise ValidationError("套餐 slug 不能为空。")
        if len(slug) > 80:
            raise ValidationError("套餐 slug 不能超过 80 个字符。")
        return slug
    def parse_bool_like(self, value):
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() not in {"", "0", "false", "off", "no"}
    def validate_plan_payload(self, payload):
        name = str(payload.get("name", "") or "").strip()
        if not name:
            raise ValidationError("套餐名称不能为空。")
        if len(name) > 80:
            raise ValidationError("套餐名称不能超过 80 个字符。")
        description = str(payload.get("description", "") or "").strip()
        if len(description) > 1000:
            raise ValidationError("套餐说明不能超过 1000 个字符。")
        slug_source = str(payload.get("slug", "") or "").strip() or name
        slug = self._panel.slugify_plan_value(slug_source)

        try:
            price_fen = int(str(payload.get("price_fen", "") or "").strip())
        except ValueError as exc:
            raise ValidationError("套餐价格必须是正整数分。") from exc
        if price_fen <= 0:
            raise ValidationError("套餐价格必须大于 0。")

        try:
            duration_days = int(str(payload.get("duration_days", "") or "").strip())
        except ValueError as exc:
            raise ValidationError("套餐时长必须是正整数天。") from exc
        if duration_days <= 0:
            raise ValidationError("套餐时长必须大于 0。")

        traffic_limit_bytes = parse_data_size(payload.get("traffic_limit"), "套餐流量")
        if traffic_limit_bytes is None:
            raise ValidationError("套餐流量不能为空。")

        try:
            sort_order = int(str(payload.get("sort_order", "0") or "0").strip())
        except ValueError as exc:
            raise ValidationError("排序值必须是整数。") from exc

        return {
            "slug": slug,
            "name": name,
            "description": description,
            "price_fen": price_fen,
            "currency": "CNY",
            "duration_days": duration_days,
            "traffic_limit_bytes": traffic_limit_bytes,
            "enabled": 1 if self._panel.parse_bool_like(payload.get("enabled", True)) else 0,
            "sort_order": sort_order,
        }
    def serialize_plan_row(self, row):
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["price_display"] = f"¥{int(item['price_fen']) / 100:.2f}"
        item["traffic_limit_display"] = human_bytes(item["traffic_limit_bytes"])
        item["status_label"] = "上架中" if item["enabled"] else "已下架"
        return item
    def query_plans(self, public_only=False):
        with self._panel.connect() as conn:
            sql = """
                SELECT
                    id,
                    slug,
                    name,
                    description,
                    price_fen,
                    currency,
                    duration_days,
                    traffic_limit_bytes,
                    enabled,
                    sort_order,
                    created_at,
                    updated_at
                FROM plans
            """
            params = []
            if public_only:
                sql += " WHERE enabled = 1"
            sql += " ORDER BY enabled DESC, sort_order ASC, id ASC"
            rows = conn.execute(sql, params).fetchall()
        return [self._panel.serialize_plan_row(row) for row in rows]
    def get_plan_by_slug(self, slug, public_only=False):
        with self._panel.connect() as conn:
            sql = """
                SELECT
                    id,
                    slug,
                    name,
                    description,
                    price_fen,
                    currency,
                    duration_days,
                    traffic_limit_bytes,
                    enabled,
                    sort_order,
                    created_at,
                    updated_at
                FROM plans
                WHERE slug = ?
            """
            params = [str(slug or "").strip()]
            if public_only:
                sql += " AND enabled = 1"
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return self._panel.serialize_plan_row(row)
    def get_plan_by_id(self, plan_id):
        with self._panel.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    slug,
                    name,
                    description,
                    price_fen,
                    currency,
                    duration_days,
                    traffic_limit_bytes,
                    enabled,
                    sort_order,
                    created_at,
                    updated_at
                FROM plans
                WHERE id = ?
                """,
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return self._panel.serialize_plan_row(row)
    def create_plan(self, payload):
        def operation(conn):
            now = utc_iso_now()
            conn.execute(
                """
                INSERT INTO plans (
                    slug, name, description, price_fen, currency,
                    duration_days, traffic_limit_bytes, enabled, sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["slug"],
                    payload["name"],
                    payload["description"],
                    payload["price_fen"],
                    payload["currency"],
                    payload["duration_days"],
                    payload["traffic_limit_bytes"],
                    payload["enabled"],
                    payload["sort_order"],
                    now,
                    now,
                ),
            )

        self._panel.apply_state_update(operation)
    def update_plan(self, plan_id, payload):
        def operation(conn):
            existing = conn.execute("SELECT id FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if existing is None:
                raise ValidationError("套餐不存在。")
            conn.execute(
                """
                UPDATE plans
                SET slug = ?, name = ?, description = ?, price_fen = ?, currency = ?,
                    duration_days = ?, traffic_limit_bytes = ?, enabled = ?, sort_order = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["slug"],
                    payload["name"],
                    payload["description"],
                    payload["price_fen"],
                    payload["currency"],
                    payload["duration_days"],
                    payload["traffic_limit_bytes"],
                    payload["enabled"],
                    payload["sort_order"],
                    utc_iso_now(),
                    plan_id,
                ),
            )

        self._panel.apply_state_update(operation)
    def create_customer(self, email, password):
        normalized_email = normalize_customer_email(email)
        raw_password = self._panel.validate_customer_password(password)

        def operation(conn):
            now = utc_iso_now()
            password_hash = generate_password_hash(raw_password)
            conn.execute(
                """
                INSERT INTO customers (
                    email, password_hash, status, created_at, updated_at
                ) VALUES (?, ?, 'active', ?, ?)
                """,
                (normalized_email, password_hash, now, now),
            )

        self._panel.apply_state_update(operation)
    def get_customer_by_email(self, email):
        normalized_email = normalize_customer_email(email)
        with self._panel.connect() as conn:
            row = conn.execute(
                """
                SELECT id, email, password_hash, status, created_at, updated_at, last_login_at
                FROM customers
                WHERE email = ?
                LIMIT 1
                """,
                (normalized_email,),
            ).fetchone()
        return dict(row) if row is not None else None
    def get_customer_by_id(self, customer_id):
        with self._panel.connect() as conn:
            row = conn.execute(
                """
                SELECT id, email, password_hash, status, created_at, updated_at, last_login_at
                FROM customers
                WHERE id = ?
                LIMIT 1
                """,
                (customer_id,),
            ).fetchone()
        return dict(row) if row is not None else None
    def touch_customer_login(self, customer_id):
        def operation(conn):
            conn.execute(
                "UPDATE customers SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (utc_iso_now(), utc_iso_now(), customer_id),
            )

        self._panel.apply_state_update(operation)
    def order_status_label(self, status):
        mapping = {
            "pending_payment": "待付款",
            "payment_submitted": "待审核",
            "payment_rejected": "已驳回",
            "fulfilled": "已开通",
            "cancelled": "已取消",
            "expired": "已过期",
        }
        return mapping.get(str(status or "").strip(), "未知")
    def order_status_tone(self, status):
        mapping = {
            "pending_payment": "warn",
            "payment_submitted": "warn",
            "payment_rejected": "bad",
            "fulfilled": "ok",
            "cancelled": "bad",
            "expired": "bad",
        }
        return mapping.get(str(status or "").strip(), "warn")
    def generate_unique_order_no(self, conn):
        date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
        for _ in range(16):
            candidate = f"ODR{date_prefix}{generate_subscription_token(8).upper()}"
            row = conn.execute("SELECT 1 FROM orders WHERE order_no = ? LIMIT 1", (candidate,)).fetchone()
            if row is None:
                return candidate
        raise RuntimeError("无法生成唯一订单号。")
    def compute_order_deadline_in_tx(self, conn, base_dt=None):
        settings = self._panel.serialize_commerce_settings(conn)
        expires_dt = (base_dt or utc_now()) + timedelta(hours=int(settings["order_expiry_hours"]))
        return expires_dt.isoformat(timespec="seconds")
    def expire_pending_orders_in_tx(self, conn):
        now_text = utc_iso_now()
        conn.execute(
            """
            UPDATE orders
            SET status = 'expired', updated_at = ?
            WHERE status IN ('pending_payment', 'payment_rejected')
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (now_text, now_text),
        )
    def expire_pending_orders(self):
        def operation(conn):
            self._panel.expire_pending_orders_in_tx(conn)

        self._panel.apply_state_update(operation)
    def create_order(self, customer_id, plan_id, kind="new_purchase", service_subscription_id=None):
        def operation(conn):
            nonlocal service_subscription_id
            self._panel.expire_pending_orders_in_tx(conn)
            customer = conn.execute(
                "SELECT id, email, status FROM customers WHERE id = ? LIMIT 1",
                (customer_id,),
            ).fetchone()
            if customer is None or customer["status"] != "active":
                raise ValidationError("客户账号不可用。")

            plan = conn.execute(
                """
                SELECT id, slug, name, description, price_fen, currency, duration_days, traffic_limit_bytes, enabled
                FROM plans
                WHERE id = ?
                LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
            if plan is None:
                raise ValidationError("套餐不存在。")
            if not int(plan["enabled"]):
                raise ValidationError("套餐已下架。")

            kind_text = str(kind or "new_purchase").strip()
            if kind_text not in {"new_purchase", "renewal"}:
                raise ValidationError("订单类型不支持。")

            if kind_text == "renewal":
                if service_subscription_id is None:
                    raise ValidationError("续费订单缺少服务实例。")
                service_row = self._panel.get_service_subscription_row_in_tx(conn, service_subscription_id, customer_id=customer_id)
                if service_row is None:
                    raise ValidationError("服务实例不存在。")
                if int(service_row["plan_id"]) != int(plan["id"]):
                    raise ValidationError("v1 仅支持按原套餐续费。")
                if not service_row["renewal_allowed"]:
                    raise ValidationError("当前服务未到续费窗口，仅已过期或流量用尽的服务可续费。")
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM orders
                    WHERE service_subscription_id = ?
                      AND status IN ('pending_payment', 'payment_submitted', 'payment_rejected')
                    LIMIT 1
                    """,
                    (service_subscription_id,),
                ).fetchone()
                if existing is not None:
                    raise ValidationError("当前服务已有未完成的续费订单。")
            else:
                service_subscription_id = None

            now_text = utc_iso_now()
            order_no = self._panel.generate_unique_order_no(conn)
            conn.execute(
                """
                INSERT INTO orders (
                    order_no, customer_id, plan_id, kind, service_subscription_id, status,
                    plan_name_snapshot, plan_description_snapshot, price_fen_snapshot, currency_snapshot,
                    duration_days_snapshot, traffic_limit_bytes_snapshot, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending_payment', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_no,
                    customer_id,
                    plan["id"],
                    kind_text,
                    service_subscription_id,
                    plan["name"],
                    plan["description"],
                    plan["price_fen"],
                    plan["currency"],
                    plan["duration_days"],
                    plan["traffic_limit_bytes"],
                    self._panel.compute_order_deadline_in_tx(conn),
                    now_text,
                    now_text,
                ),
            )
            return order_no

        return self._panel.apply_state_update(operation)
    def payment_proof_extension_from_bytes(self, payload):
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if payload.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "webp"
        return ""
    def save_payment_proof_file(self, order_no, file_storage):
        content = file_storage.read()
        if not content:
            raise ValidationError("请上传支付截图。")
        if len(content) > PAYMENT_PROOF_MAX_BYTES:
            raise ValidationError(f"支付截图不能超过 {human_bytes(PAYMENT_PROOF_MAX_BYTES)}。")
        extension = self._panel.payment_proof_extension_from_bytes(content)
        if extension not in {"png", "jpg", "webp"}:
            raise ValidationError("支付截图只支持 PNG、JPG、WEBP。")
        target_dir = PAYMENT_PROOFS_DIR / str(order_no)
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{utc_now().strftime('%Y%m%dT%H%M%S')}-{generate_subscription_token(6)}.{extension}"
        relative_path = Path("payment-proofs") / str(order_no) / filename
        absolute_path = PAYMENT_PROOFS_DIR.parent / relative_path
        absolute_path.write_bytes(content)
        return relative_path.as_posix()
    def submit_order_payment_submission(self, customer_id, order_no, file_storage, payer_note):
        note = str(payer_note or "").strip()
        if len(note) > 300:
            raise ValidationError("付款备注不能超过 300 个字符。")
        relative_path = self._panel.save_payment_proof_file(order_no, file_storage)

        def operation(conn):
            self._panel.expire_pending_orders_in_tx(conn)
            order = conn.execute(
                """
                SELECT id, order_no, customer_id, status
                FROM orders
                WHERE order_no = ? AND customer_id = ?
                LIMIT 1
                """,
                (order_no, customer_id),
            ).fetchone()
            if order is None:
                raise ValidationError("订单不存在。")
            if order["status"] not in {"pending_payment", "payment_rejected"}:
                raise ValidationError("当前订单状态不允许重新提交支付凭证。")
            now_text = utc_iso_now()
            conn.execute(
                """
                INSERT INTO order_payment_submissions (
                    order_id, proof_image_path, payer_note, submitted_at, review_note, reviewed_at, review_status
                ) VALUES (?, ?, ?, ?, '', NULL, 'submitted')
                """,
                (order["id"], relative_path, note, now_text),
            )
            conn.execute(
                """
                UPDATE orders
                SET status = 'payment_submitted', rejection_reason = '', updated_at = ?
                WHERE id = ?
                """,
                (now_text, order["id"]),
            )

        try:
            self._panel.apply_state_update(operation)
        except Exception:
            (PAYMENT_PROOFS_DIR.parent / relative_path).unlink(missing_ok=True)
            raise
    def serialize_order_row(self, row):
        item = dict(row)
        latest_submission_id = item.get("latest_submission_id")
        item["status_label"] = self._panel.order_status_label(item.get("status"))
        item["status_tone"] = self._panel.order_status_tone(item.get("status"))
        item["price_display"] = f"¥{int(item.get('price_fen_snapshot') or 0) / 100:.2f}"
        item["traffic_limit_display"] = human_bytes(item.get("traffic_limit_bytes_snapshot") or 0)
        item["expires_at_display"] = format_display_time(item.get("expires_at")) if item.get("expires_at") else "暂无"
        item["fulfilled_at_display"] = (
            format_display_time(item.get("fulfilled_at")) if item.get("fulfilled_at") else "暂无"
        )
        item["created_at_display"] = format_display_time(item.get("created_at")) if item.get("created_at") else "暂无"
        item["updated_at_display"] = format_display_time(item.get("updated_at")) if item.get("updated_at") else "暂无"
        item["payer_note"] = str(item.get("payer_note") or "").strip()
        item["review_note"] = str(item.get("review_note") or "").strip()
        item["rejection_reason"] = str(item.get("rejection_reason") or "").strip()
        item["proof_available"] = latest_submission_id is not None
        item["latest_submission_id"] = latest_submission_id
        item["proof_review_status"] = str(item.get("proof_review_status") or "").strip()
        item["proof_submitted_at_display"] = (
            format_display_time(item.get("proof_submitted_at")) if item.get("proof_submitted_at") else "暂无"
        )
        item["proof_reviewed_at_display"] = (
            format_display_time(item.get("proof_reviewed_at")) if item.get("proof_reviewed_at") else "暂无"
        )
        return item
    def get_orders_base_query(self):
        return """
            SELECT
                o.id,
                o.order_no,
                o.customer_id,
                o.plan_id,
                o.kind,
                o.service_subscription_id,
                o.status,
                o.plan_name_snapshot,
                o.plan_description_snapshot,
                o.price_fen_snapshot,
                o.currency_snapshot,
                o.duration_days_snapshot,
                o.traffic_limit_bytes_snapshot,
                o.expires_at,
                o.fulfilled_at,
                o.cancelled_at,
                o.rejection_reason,
                o.created_at,
                o.updated_at,
                c.email AS customer_email,
                p.slug AS plan_slug,
                s.port_id AS port_id,
                pt.listen_port AS listen_port,
                ops.id AS latest_submission_id,
                ops.payer_note AS payer_note,
                ops.submitted_at AS proof_submitted_at,
                ops.review_note AS review_note,
                ops.reviewed_at AS proof_reviewed_at,
                ops.review_status AS proof_review_status
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            JOIN plans p ON p.id = o.plan_id
            LEFT JOIN service_subscriptions s ON s.id = o.service_subscription_id
            LEFT JOIN ports pt ON pt.id = s.port_id
            LEFT JOIN order_payment_submissions ops
                ON ops.id = (
                    SELECT id
                    FROM order_payment_submissions
                    WHERE order_id = o.id
                    ORDER BY id DESC
                    LIMIT 1
                )
        """
    def query_customer_orders(self, customer_id):
        self._panel.expire_pending_orders()
        with self._panel.connect() as conn:
            rows = conn.execute(
                self._panel.get_orders_base_query()
                + """
                WHERE o.customer_id = ?
                ORDER BY o.id DESC
                """,
                (customer_id,),
            ).fetchall()
        return [self._panel.serialize_order_row(row) for row in rows]
    def get_customer_order(self, customer_id, order_no):
        self._panel.expire_pending_orders()
        with self._panel.connect() as conn:
            row = conn.execute(
                self._panel.get_orders_base_query()
                + """
                WHERE o.customer_id = ? AND o.order_no = ?
                LIMIT 1
                """,
                (customer_id, order_no),
            ).fetchone()
        if row is None:
            return None
        return self._panel.serialize_order_row(row)
    def query_admin_orders(self, status_filter=""):
        self._panel.expire_pending_orders()
        with self._panel.connect() as conn:
            sql = self._panel.get_orders_base_query()
            params = []
            if status_filter:
                sql += " WHERE o.status = ?"
                params.append(status_filter)
            sql += " ORDER BY o.id DESC LIMIT 100"
            rows = conn.execute(sql, params).fetchall()
        return [self._panel.serialize_order_row(row) for row in rows]
    def get_admin_order(self, order_id):
        self._panel.expire_pending_orders()
        with self._panel.connect() as conn:
            row = conn.execute(
                self._panel.get_orders_base_query()
                + """
                WHERE o.id = ?
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return self._panel.serialize_order_row(row)
    def get_payment_submission_record(self, submission_id):
        with self._panel.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    ops.id,
                    ops.order_id,
                    ops.proof_image_path,
                    o.customer_id
                FROM order_payment_submissions ops
                JOIN orders o ON o.id = ops.order_id
                WHERE ops.id = ?
                LIMIT 1
                """,
                (submission_id,),
            ).fetchone()
        return dict(row) if row is not None else None
    def query_commerce_overview(self):
        self._panel.expire_pending_orders()
        with self._panel.connect() as conn:
            customer_count = int(conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0])
            enabled_plan_count = int(conn.execute("SELECT COUNT(*) FROM plans WHERE enabled = 1").fetchone()[0])
            service_count = int(conn.execute("SELECT COUNT(*) FROM service_subscriptions").fetchone()[0])
            pending_review_count = int(
                conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'payment_submitted'").fetchone()[0]
            )
        return {
            "customer_count": customer_count,
            "enabled_plan_count": enabled_plan_count,
            "service_count": service_count,
            "pending_review_count": pending_review_count,
        }
    def get_service_subscription_row_in_tx(self, conn, service_subscription_id, customer_id=None):
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        sql = """
            SELECT
                s.id,
                s.customer_id,
                s.plan_id,
                s.port_id,
                s.source_order_id,
                s.latest_order_id,
                s.created_at,
                s.updated_at,
                p.name AS plan_name,
                p.slug AS plan_slug,
                pt.listen_port,
                pt.note,
                pt.enabled,
                pt.expires_at,
                pt.traffic_limit_bytes,
                pt.tenant_token,
                pt.subscription_token,
                pt.tenant_username,
                pt.tenant_password,
                COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                COALESCE(t.total_bytes_received, 0) AS total_bytes_received,
                COALESCE(d.total_bytes_sent, 0) AS today_bytes_sent,
                COALESCE(d.total_bytes_received, 0) AS today_bytes_received,
                t.last_seen AS last_seen
            FROM service_subscriptions s
            JOIN plans p ON p.id = s.plan_id
            LEFT JOIN ports pt ON pt.id = s.port_id
            LEFT JOIN traffic_totals t ON t.listen_port = pt.listen_port
            LEFT JOIN traffic_daily d ON d.listen_port = pt.listen_port AND d.stat_date = ?
            WHERE s.id = ?
        """
        params = [today, service_subscription_id]
        if customer_id is not None:
            sql += " AND s.customer_id = ?"
            params.append(customer_id)
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["traffic_usage_bytes"] = int(item.get("total_bytes_sent") or 0) + int(item.get("total_bytes_received") or 0)
        status = status_payload(
            bool(item.get("enabled")),
            item.get("expires_at"),
            item.get("traffic_limit_bytes"),
            item["traffic_usage_bytes"],
        )
        item["status"] = status["code"]
        item["status_label"] = status["label"]
        item["renewal_allowed"] = item["status"] in {"expired", "quota"}
        item["expires_at_display"] = format_display_time(item.get("expires_at"))
        item["traffic_limit_display"] = (
            human_bytes(item.get("traffic_limit_bytes")) if item.get("traffic_limit_bytes") is not None else "无限制"
        )
        item["traffic_used_display"] = human_bytes(item["traffic_usage_bytes"])
        item["last_seen_display"] = format_display_time(item.get("last_seen")) if item.get("last_seen") else "暂无"
        return item
    def query_customer_service_subscriptions(self, customer_id):
        with self._panel.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM service_subscriptions WHERE customer_id = ? ORDER BY id DESC",
                (customer_id,),
            ).fetchall()
            return [self._panel.get_service_subscription_row_in_tx(conn, row["id"], customer_id=customer_id) for row in rows]
    def get_customer_service_subscription(self, customer_id, service_subscription_id):
        with self._panel.connect() as conn:
            return self._panel.get_service_subscription_row_in_tx(conn, service_subscription_id, customer_id=customer_id)
    def allocate_auto_port_in_tx(self, conn):
        if COMMERCE_AUTO_PORT_START is None or COMMERCE_AUTO_PORT_END is None:
            raise ValidationError("商业化自动分配端口范围未配置。")
        rows = conn.execute(
            """
            SELECT listen_port
            FROM ports
            WHERE listen_port BETWEEN ? AND ?
            ORDER BY listen_port ASC
            """,
            (COMMERCE_AUTO_PORT_START, COMMERCE_AUTO_PORT_END),
        ).fetchall()
        used = {int(row["listen_port"]) for row in rows}
        for listen_port in range(COMMERCE_AUTO_PORT_START, COMMERCE_AUTO_PORT_END + 1):
            if listen_port not in used:
                return listen_port
        raise ValidationError("自动分配端口范围已耗尽，请扩容可售端口区间。")
    def compute_service_expiry(self, duration_days):
        return (utc_now() + timedelta(days=int(duration_days))).isoformat(timespec="seconds")
    def build_service_note(self, customer_email, plan_name):
        base = f"{plan_name} / {customer_email}"
        if len(base) <= 200:
            return base
        return base[:200]
    def mark_latest_payment_submission_reviewed_in_tx(self, conn, order_id, review_status, review_note):
        latest_submission = conn.execute(
            """
            SELECT id
            FROM order_payment_submissions
            WHERE order_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()
        if latest_submission is None:
            raise ValidationError("订单缺少支付凭证。")
        conn.execute(
            """
            UPDATE order_payment_submissions
            SET review_status = ?, review_note = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (review_status, str(review_note or "").strip(), utc_iso_now(), latest_submission["id"]),
        )
    def fulfill_order(self, order_id, review_note=""):
        note_text = str(review_note or "").strip()
        if len(note_text) > 300:
            raise ValidationError("审核备注不能超过 300 个字符。")

        try:
            # Acquire the same cross-process lock before opening the SQLite
            # write transaction. The resident AI manager follows this order as
            # well, avoiding a DB-lock/file-lock inversion during a fulfilment.
            with exclusive_file_lock(self._panel._ai_manager_apply_lock_path()):
                with self._panel.write_lock:
                    self._panel.sync_traffic_state_locked()
                    with self._panel.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            self._panel.expire_pending_orders_in_tx(conn)
                            order = conn.execute(
                                """
                                SELECT
                                    o.*,
                                    c.email AS customer_email
                                FROM orders o
                                JOIN customers c ON c.id = o.customer_id
                                WHERE o.id = ?
                                LIMIT 1
                                """,
                                (order_id,),
                            ).fetchone()
                            if order is None:
                                raise ValidationError("订单不存在。")
                            if order["status"] != "payment_submitted":
                                raise ValidationError("当前订单状态不允许审核开通。")

                            now_text = utc_iso_now()
                            service_expires_at = self._panel.compute_service_expiry(order["duration_days_snapshot"])
                            service_subscription_id = order["service_subscription_id"]

                            if order["kind"] == "new_purchase":
                                listen_port = self._panel.allocate_auto_port_in_tx(conn)
                                conn.execute(
                                    """
                                    INSERT INTO ports (
                                        listen_port, upstream_host, upstream_port,
                                        tenant_token, subscription_token, tenant_username, tenant_password,
                                        expires_at, traffic_limit_bytes, enabled, note,
                                        customer_id, service_subscription_id, source_order_id,
                                        created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?, ?)
                                    """,
                                    (
                                        listen_port,
                                        DEFAULT_UPSTREAM_HOST,
                                        DEFAULT_UPSTREAM_PORT,
                                        self._panel.generate_unique_port_token(conn, "tenant_token"),
                                        self._panel.generate_unique_port_token(conn, "subscription_token"),
                                        self._panel.generate_unique_tenant_username(conn),
                                        generate_tenant_password(),
                                        service_expires_at,
                                        order["traffic_limit_bytes_snapshot"],
                                        self._panel.build_service_note(
                                            order["customer_email"], order["plan_name_snapshot"]
                                        ),
                                        order["customer_id"],
                                        order["id"],
                                        now_text,
                                        now_text,
                                    ),
                                )
                                port_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                                conn.execute(
                                    """
                                    INSERT INTO service_subscriptions (
                                        customer_id, plan_id, port_id, source_order_id, latest_order_id, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        order["customer_id"],
                                        order["plan_id"],
                                        port_id,
                                        order["id"],
                                        order["id"],
                                        now_text,
                                        now_text,
                                    ),
                                )
                                service_subscription_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                                conn.execute(
                                    """
                                    UPDATE ports
                                    SET service_subscription_id = ?
                                    WHERE id = ?
                                    """,
                                    (service_subscription_id, port_id),
                                )
                            else:
                                service_row = self._panel.get_service_subscription_row_in_tx(
                                    conn,
                                    order["service_subscription_id"],
                                    customer_id=order["customer_id"],
                                )
                                if service_row is None or service_row.get("port_id") is None:
                                    raise ValidationError("续费目标服务不存在。")
                                port_id = int(service_row["port_id"])
                                conn.execute(
                                    """
                                    UPDATE ports
                                    SET expires_at = ?, traffic_limit_bytes = ?, enabled = 1, updated_at = ?
                                    WHERE id = ?
                                    """,
                                    (
                                        service_expires_at,
                                        order["traffic_limit_bytes_snapshot"],
                                        now_text,
                                        port_id,
                                    ),
                                )
                                self._panel.reset_port_usage_in_tx(conn, service_row["listen_port"])
                                conn.execute(
                                    """
                                    UPDATE service_subscriptions
                                    SET latest_order_id = ?, plan_id = ?, updated_at = ?
                                    WHERE id = ?
                                    """,
                                    (
                                        order["id"],
                                        order["plan_id"],
                                        now_text,
                                        order["service_subscription_id"],
                                    ),
                                )
                                service_subscription_id = order["service_subscription_id"]

                            self._panel.mark_latest_payment_submission_reviewed_in_tx(
                                conn, order["id"], "approved", note_text
                            )
                            conn.execute(
                                """
                                UPDATE orders
                                SET status = 'fulfilled',
                                    fulfilled_at = ?,
                                    rejection_reason = '',
                                    service_subscription_id = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (now_text, service_subscription_id, now_text, order["id"]),
                            )
                            self._panel.disable_auto_stopped_ports_in_tx(conn)
                            self._panel._persist_and_reload_locked(conn, reload_xray=True)
                        except Exception:
                            conn.rollback()
                            raise
        except LockBusyError as exc:
            self._panel._raise_apply_lock_busy(exc)
    def reject_order(self, order_id, review_note=""):
        note_text = str(review_note or "").strip()
        if not note_text:
            raise ValidationError("驳回订单时必须填写原因。")
        if len(note_text) > 300:
            raise ValidationError("驳回原因不能超过 300 个字符。")

        def operation(conn):
            self._panel.expire_pending_orders_in_tx(conn)
            order = conn.execute(
                "SELECT id, status FROM orders WHERE id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            if order is None:
                raise ValidationError("订单不存在。")
            if order["status"] != "payment_submitted":
                raise ValidationError("当前订单状态不允许驳回。")
            self._panel.mark_latest_payment_submission_reviewed_in_tx(conn, order["id"], "rejected", note_text)
            conn.execute(
                """
                UPDATE orders
                SET status = 'payment_rejected', rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (note_text, utc_iso_now(), order["id"]),
            )

        self._panel.apply_state_update(operation)
    def cancel_order(self, order_id, review_note=""):
        note_text = str(review_note or "").strip()
        if len(note_text) > 300:
            raise ValidationError("取消备注不能超过 300 个字符。")

        def operation(conn):
            self._panel.expire_pending_orders_in_tx(conn)
            order = conn.execute(
                "SELECT id, status FROM orders WHERE id = ? LIMIT 1",
                (order_id,),
            ).fetchone()
            if order is None:
                raise ValidationError("订单不存在。")
            if order["status"] in {"fulfilled", "cancelled", "expired"}:
                raise ValidationError("当前订单状态不允许取消。")
            conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled', cancelled_at = ?, rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (utc_iso_now(), note_text, utc_iso_now(), order["id"]),
            )

        self._panel.apply_state_update(operation)
