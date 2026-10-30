from __future__ import annotations

import pandas as pd

from cli_output import display_width, pad_display, prepare_table_display, render_table


def test_display_width_cjk() -> None:
    assert display_width("油气开采") == 8
    assert display_width("881289") == 6


def test_pad_display_aligns_cjk() -> None:
    padded = pad_display("航天装备", 18)
    assert display_width(padded) == 18


def test_render_table_name_column_min_width() -> None:
    df = pd.DataFrame(
        {
            "code": ["881289"],
            "name": ["航天装备"],
            "close_end": [1591.0699462890625],
            "chg_3d": [5.15],
            "rank_3d": [1],
        }
    )
    text = render_table(df)
    assert "1591.07" in text
    assert "航天装备" in text
    lines = text.splitlines()
    assert any("+" in line and "-" in line for line in lines)


def test_prepare_table_display_does_not_mutate_input() -> None:
    df = pd.DataFrame({"name": ["测试"], "close_end": [1.234567]})
    original = df.copy()
    _ = prepare_table_display(df)
    pd.testing.assert_frame_equal(df, original)
