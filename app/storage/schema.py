"""Base database schema bootstrap for the panel application."""

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SchemaBootstrap:
    """Install shared storage schema and invoke domain-owned schema hooks."""

    def __init__(self, base_schema=BASE_SCHEMA):
        self.base_schema = base_schema

    def initialize(self, database, schema_initializers=()):
        """Initialize all schema pieces on one connection.

        Domain services own their tables and migrations.  The bootstrap only
        supplies one connection and preserves the explicit hook order chosen
        by the application lifecycle.
        """
        with database.connect() as conn:
            conn.executescript(self.base_schema)
            for initializer in schema_initializers:
                initializer(conn)
