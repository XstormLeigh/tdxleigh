"""CLI output helpers: JSON/CSV via easy_tdx, tables with CJK-aware alignment."""

from __future__ import annotations

from typing import Final

import click
import pandas as pd
from tabulate import tabulate
from wcwidth import wcswidth

NAME_COLUMNS: Final[frozenset[str]] = frozenset({"name", "board_name"})
NAME_MIN_DISPLAY_WIDTH: Final[int] = 18


def display_width(text: str) -> int:
    """Terminal display width (full-width CJK counts as 2)."""
    measured = wcswidth(str(text))
    if measured < 0:
        return len(str(text))
    return measured


def pad_display(text: str, width: int) -> str:
    """Pad with ASCII spaces so display width equals *width*."""
    s = str(text)
    gap = width - display_width(s)
    if gap <= 0:
        return s
    return s + (" " * gap)


def _format_cell(column: str, value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if column.startswith("rank_"):
        return str(int(value))
    if column.startswith("chg_"):
        return f"{float(value):.2f}"
    if column in {"close_end", "avg_rank", "score"}:
        return f"{float(value):.2f}"
    if column.startswith("r2_"):
        return f"{float(value):.4f}"
    if column.startswith("beta_"):
        return f"{float(value):.4f}"
    if column == "amount":
        return f"{float(value):.0f}"
    if column == "amount_mn":
        return f"{float(value):,.1f}"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def prepare_table_display(df: pd.DataFrame) -> pd.DataFrame:
    """Format values and pad name columns for fixed-width terminal tables."""
    if df.empty:
        return df.copy()

    display = df.copy()
    for col in display.columns:
        display[col] = display[col].map(lambda v, c=col: _format_cell(c, v))

    for col in display.columns:
        if col not in NAME_COLUMNS:
            continue
        header_w = display_width(col)
        cell_w = max(display_width(str(v)) for v in display[col])
        target = max(header_w, cell_w, NAME_MIN_DISPLAY_WIDTH)
        display[col] = display[col].map(lambda v, w=target: pad_display(v, w))

    return display


def render_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(无数据)"

    display = prepare_table_display(df)
    colalign = tuple("left" if c in NAME_COLUMNS else "right" for c in display.columns)
    return str(
        tabulate(
            display,
            headers="keys",
            tablefmt="grid",
            showindex=False,
            colalign=colalign,
        )
    )


def print_output(df: pd.DataFrame, fmt: str = "json") -> None:
    """Same contract as ``easy_tdx.cli.output.print_output``; table uses local renderer."""
    if fmt == "table":
        text = render_table(df)
    else:
        from easy_tdx.cli.output import format_output

        text = format_output(df, fmt)
    if text:
        click.echo(text)
