"""Cumulative order return settlement and administrative decisions."""
from pathlib import Path
from alembic import op
revision="0009"
down_revision="0008"
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())
def downgrade():
    raise RuntimeError("Financial history must not be downgraded destructively")
