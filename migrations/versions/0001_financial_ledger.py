"""Tenant-isolated immutable ledger and guarded reservations."""
from pathlib import Path
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})


def downgrade():
    raise RuntimeError("Financial data cannot be dropped automatically; use a reviewed recovery plan.")
