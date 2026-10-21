"""Return reversals and administrative review without bonus debt."""
from pathlib import Path
from alembic import op
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

def upgrade():
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})

def downgrade():
    raise RuntimeError("Review financial retention before downgrading.")
