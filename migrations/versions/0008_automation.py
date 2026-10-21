"""Durable automation configuration, monitors and initial accrual proofs."""
from pathlib import Path
from alembic import op
revision="0008"
down_revision="0007"
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text(),execution_options={"no_parameters":True})
def downgrade():
    raise RuntimeError("Financial accrual evidence must be preserved.")
