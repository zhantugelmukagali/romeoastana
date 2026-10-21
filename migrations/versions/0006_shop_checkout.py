"""Versioned shop rules and immutable customer/order bindings."""
from pathlib import Path
from alembic import op
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text(),
                                 execution_options={"no_parameters": True})

def downgrade():
    raise RuntimeError("Review retained rule versions and order bindings before downgrading.")
