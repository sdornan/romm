"""Which version of a game the grouped gallery shows by default.

Grouping collapses every regional dump of a game into one entry, so one of them
has to represent the group. Ordering by filename put "(Beta)", "(Demo)" and
"(Europe)" ahead of a US retail dump, which is what #1528 reported: the default
version was effectively whichever name sorted first.

The window now ranks on the scalar columns `ParsedTags.rom_columns` derives, so
these tests build their roms the way the scan does -- from the filename -- and
assert on the version the query picks.
"""

from unittest.mock import MagicMock, patch

import pytest

from handler.database import db_rom_handler
from handler.filesystem import fs_rom_handler
from models.platform import Platform
from models.rom import Rom
from models.user import User

# Every rom in a test shares this id, which is what puts them in one group.
GROUP_IGDB_ID = 424242


def _add_version(platform: Platform, fs_name: str) -> Rom:
    """Add one version of the group's game, exactly as a scan would."""
    parsed_tags = fs_rom_handler.parse_tags(fs_name)
    return db_rom_handler.add_rom(
        Rom(
            platform_id=platform.id,
            igdb_id=GROUP_IGDB_ID,
            name="Test Game",
            slug="test-game",
            fs_name=fs_name,
            fs_path=f"{platform.slug}/roms",
            **parsed_tags.rom_columns,
        )
    )


def _default_version(platform: Platform, region_priority: list[str]) -> str:
    """The fs_name of the version the grouped gallery would show."""
    config = MagicMock(SCAN_REGION_PRIORITY=region_priority)
    with patch("handler.database.roms_handler.cm.get_config", return_value=config):
        roms = db_rom_handler.get_roms_scalar(
            platform_ids=[platform.id],
            group_by_meta_id=True,
            order_by="name",
            order_dir="asc",
        )

    assert len(roms) == 1, "the versions should collapse into a single group"
    return roms[0].fs_name


def test_prefers_the_configured_region(platform: Platform):
    for fs_name in [
        "Test Game (Europe).zip",
        "Test Game (Japan).zip",
        "Test Game (USA).zip",
    ]:
        _add_version(platform, fs_name)

    assert _default_version(platform, ["us", "eu", "jp"]) == "Test Game (USA).zip"
    assert _default_version(platform, ["jp", "eu", "us"]) == "Test Game (Japan).zip"
    assert _default_version(platform, ["eu", "us", "jp"]) == "Test Game (Europe).zip"


def test_region_priority_change_needs_no_rescan(platform: Platform):
    """The ranking is built per query, so a new priority applies immediately."""
    for fs_name in ["Test Game (Europe).zip", "Test Game (USA).zip"]:
        _add_version(platform, fs_name)

    assert _default_version(platform, ["us"]) == "Test Game (USA).zip"
    assert _default_version(platform, ["eu"]) == "Test Game (Europe).zip"


@pytest.mark.parametrize(
    "prerelease_fs_name",
    [
        "Test Game (USA) (Beta).zip",
        "Test Game (USA) (Demo).zip",
        "Test Game (USA) (Kiosk Demo).zip",
        "Test Game (USA) (Proto).zip",
        "Test Game (USA) (Sample).zip",
    ],
)
def test_a_retail_release_beats_a_prerelease(
    platform: Platform, prerelease_fs_name: str
):
    """Even in a more preferred region: a demo is never the default."""
    _add_version(platform, prerelease_fs_name)
    _add_version(platform, "Test Game (Japan).zip")

    assert _default_version(platform, ["us", "jp"]) == "Test Game (Japan).zip"


def test_prefers_the_latest_revision(platform: Platform):
    for fs_name in [
        "Test Game (USA).zip",
        "Test Game (USA) (Rev 1).zip",
        "Test Game (USA) (Rev 2).zip",
    ]:
        _add_version(platform, fs_name)

    assert _default_version(platform, ["us"]) == "Test Game (USA) (Rev 2).zip"


def test_region_outranks_revision(platform: Platform):
    """A revised dump of a less preferred region does not win the group."""
    _add_version(platform, "Test Game (Europe) (Rev 2).zip")
    _add_version(platform, "Test Game (USA).zip")

    assert _default_version(platform, ["us", "eu"]) == "Test Game (USA).zip"


def test_untagged_regions_rank_last(platform: Platform):
    _add_version(platform, "Test Game.zip")
    _add_version(platform, "Test Game (Japan).zip")

    assert _default_version(platform, ["us", "jp"]) == "Test Game (Japan).zip"


def test_filename_still_breaks_a_tie(platform: Platform):
    """Two dumps the ranking cannot separate keep a stable order.

    Alphabetically the shorter name comes first, so an untagged dump wins over
    the same dump carrying an extra tag.
    """
    _add_version(platform, "Test Game (USA) (Alt).zip")
    _add_version(platform, "Test Game (USA).zip")

    assert _default_version(platform, ["us"]) == "Test Game (USA).zip"


def test_main_sibling_overrides_the_ranking(platform: Platform, admin_user: User):
    """A user's explicit pick still wins over every derived signal."""
    demo = _add_version(platform, "Test Game (Japan) (Demo).zip")
    _add_version(platform, "Test Game (USA).zip")

    rom_user = db_rom_handler.add_rom_user(demo.id, admin_user.id)
    db_rom_handler.update_rom_user(rom_user.id, {"is_main_sibling": True})

    config = MagicMock(SCAN_REGION_PRIORITY=["us", "jp"])
    with patch("handler.database.roms_handler.cm.get_config", return_value=config):
        roms = db_rom_handler.get_roms_scalar(
            platform_ids=[platform.id],
            user_id=admin_user.id,
            group_by_meta_id=True,
            order_by="name",
            order_dir="asc",
        )

    assert [rom.fs_name for rom in roms] == ["Test Game (Japan) (Demo).zip"]
