from pathlib import Path
from alembic import op
revision="0014"
down_revision="0013"
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text(),execution_options={"no_parameters":True})
def downgrade():raise RuntimeError("Order customer binding history must be preserved")
