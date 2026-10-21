from pathlib import Path
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(),
        execution_options={"no_parameters": True},
    )

def downgrade():
    raise RuntimeError("Presale cancellation requests, receipts and financial history must be preserved")
