"""Add the scalar sibling-ranking columns and cover them in the dedup index

Grouping collapses every version of a game into one gallery entry, and the
version it shows was picked by `is_main_sibling` (a per-user manual flag) and
then alphabetically by filename. Alphabetical order lands on "(Beta)",
"(Demo)" and "(Europe)" ahead of a US retail dump, so the default version is
effectively arbitrary.

Ranking on the region and tag columns directly is not an option: they are JSON
(LONGTEXT on MariaDB), so reading them in the dedup window drops it off
`idx_roms_sibling_cover` and back to the full scan of the wide `roms` row that
0107 fixed. These three columns mirror the parts the window sorts on as plain
scalars, and join the covering index so the window still reads index-only.

`primary_region` stays a name rather than a rank so no stored value depends on
the configured region priority: the ranking CASE is built per query from
`scan.priority.region`, so changing that setting takes effect immediately with
nothing to recompute.

Existing rows get the column defaults (no region, not a prerelease, unrevised)
until their tags are re-read by a Complete rescan, matching how the region tag
normalization in 5.2.0 reaches rows scanned before it.

Revision ID: 0108_sibling_ranking_columns
Revises: 0107_roms_dedup_cover_index
Create Date: 2026-08-20 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision = "0108_sibling_ranking_columns"
down_revision = "0107_roms_dedup_cover_index"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_roms_sibling_cover"

# The 0107 index, which the downgrade restores.
OLD_INDEX_COLUMNS = [
    "platform_id",
    "igdb_id",
    "moby_id",
    "ss_id",
    "launchbox_id",
    "ra_id",
    "hasheous_id",
    "tgdb_id",
    "flashpoint_id",
    "fs_name_no_ext",
    "id",
]

# The new sort inputs are appended, so every existing consumer of the index
# keeps the prefix it already reads.
NEW_COLUMNS = ["is_prerelease", "primary_region", "revision_rank"]
NEW_INDEX_COLUMNS = OLD_INDEX_COLUMNS + NEW_COLUMNS


def upgrade() -> None:
    with op.batch_alter_table("roms", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("primary_region", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("is_prerelease", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("revision_rank", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.drop_index(INDEX_NAME, if_exists=True)
        batch_op.create_index(
            INDEX_NAME,
            NEW_INDEX_COLUMNS,
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("roms", schema=None) as batch_op:
        batch_op.drop_index(INDEX_NAME, if_exists=True)
        batch_op.create_index(
            INDEX_NAME,
            OLD_INDEX_COLUMNS,
            unique=False,
            if_not_exists=True,
        )
        for column in NEW_COLUMNS:
            batch_op.drop_column(column)
