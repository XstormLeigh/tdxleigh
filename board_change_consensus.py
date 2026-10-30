"""板块多周期共振排行 — 取 3/5/10 日涨幅榜 Top-N 的交集。

等价于分别运行::

    easy-tdx board-change-ranking --table --type HY --days 3  --top 10
    easy-tdx board-change-ranking --table --type HY --days 5  --top 10
    easy-tdx board-change-ranking --table --type HY --days 10 --top 10

然后人工比对三张榜单的共同板块名称。本脚本每个板块只取一次日 K 线
（长度 = max(windows) + 缓冲），一次性算出全部窗口的涨跌幅，因此比连跑
三次原命令快约 3 倍。

用法::

    python board_change_consensus.py --type HY --days 3,5,10 --top 10 --table
    python board_change_consensus.py --type GN --names-only

需在安装了 easy-tdx 的解释器下运行，例如::

    D:/miniconda3/envs/py312/python.exe board_change_consensus.py --table
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from typing import Any, Final, Protocol

import click
import pandas as pd

from easy_tdx.cli.conn import get_mac_client
from cli_output import print_output
from easy_tdx.cli.parsers import parse_board_type
from easy_tdx.mac.enums import Adjust, BoardType, Period

DEFAULT_WINDOWS: Final[tuple[int, ...]] = (3, 5, 10)
BASE_COLUMNS: Final[tuple[str, ...]] = ("code", "name", "close_end")

# 与 easy_tdx.mac.client.get_board_change_ranking 保持一致的节假日缓冲。
_KLINE_BUFFER: Final[int] = 10


class BoardKlineSource(Protocol):
    """``MacClient`` 中本脚本实际用到的最小接口（便于测试时替身注入）。"""

    def get_board_list(self, board_type: BoardType, /) -> pd.DataFrame: ...

    def get_stock_kline(
        self,
        *,
        market: int,
        code: str,
        period: Period,
        count: int,
        adjust: Adjust,
    ) -> pd.DataFrame: ...


def parse_windows(raw: str) -> tuple[int, ...]:
    """解析 ``"3,5,10"`` 形式的窗口参数，返回去重升序的交易日数元组。"""
    parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
    if not parts:
        raise ValueError("--days 不能为空，示例: --days 3,5,10")

    windows: set[int] = set()
    for part in parts:
        if not part.lstrip("-").isdigit():
            raise ValueError(f"--days 含非法值: {part!r}")
        value = int(part)
        if value < 1:
            raise ValueError(f"--days 必须 >= 1，got {value}")
        windows.add(value)
    return tuple(sorted(windows))


def change_pct_columns(windows: Sequence[int]) -> list[str]:
    """窗口涨跌幅列名。"""
    return [f"chg_{w}d" for w in windows]


def rank_columns(windows: Sequence[int]) -> list[str]:
    """窗口排名列名。"""
    return [f"rank_{w}d" for w in windows]


def _resolve_end_position(kline_df: pd.DataFrame, target_ts: pd.Timestamp | None) -> int | None:
    """定位截止日在已排序 K 线中的行号；无可用数据时返回 ``None``。"""
    if target_ts is None:
        return len(kline_df) - 1
    mask = kline_df["datetime"] <= target_ts
    if not mask.any():
        return None
    return int(mask[mask].index[-1])


def _target_timestamp(target_date: int | None) -> pd.Timestamp | None:
    """将 ``YYYYMMDD`` 整数转为 ``Timestamp``。"""
    if target_date is None:
        return None
    return pd.Timestamp(
        year=target_date // 10000,
        month=(target_date // 100) % 100,
        day=target_date % 100,
    )


def compute_window_changes(
    kline_df: pd.DataFrame,
    windows: Sequence[int],
    target_ts: pd.Timestamp | None = None,
) -> dict[str, float] | None:
    """由单个板块的日 K 线算出各窗口涨跌幅。

    Args:
        kline_df: 单板块日 K 线，至少含 ``datetime`` 与 ``close`` 两列。
        windows: 回溯交易日数序列，如 ``(3, 5, 10)``。
        target_ts: 截止日，``None`` 表示用最后一根 K 线。

    Returns:
        ``{"close_end": ..., "chg_3d": ..., ...}``；数据不足时返回 ``None``。
        涨跌幅口径与 ``board-change-ranking`` 完全一致：
        ``(close[end] - close[end - w]) / close[end - w] * 100``，
        起点行号在 0 处截断。
    """
    if kline_df.empty or len(kline_df) < 2:
        return None

    ordered = kline_df.sort_values("datetime").reset_index(drop=True)
    end_pos = _resolve_end_position(ordered, target_ts)
    if end_pos is None or end_pos < 1:
        return None

    closes = ordered["close"].to_numpy(dtype="float64")
    close_end = float(closes[end_pos])

    result: dict[str, float] = {"close_end": close_end}
    for window in windows:
        close_start = float(closes[max(0, end_pos - window)])
        if close_start == 0.0:
            return None
        result[f"chg_{window}d"] = round((close_end - close_start) / close_start * 100, 2)
    return result


def fetch_board_change_matrix(
    client: BoardKlineSource,
    board_type: BoardType,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    target_date: int | None = None,
    *,
    progress: bool = False,
) -> pd.DataFrame:
    """拉取板块日 K 线并组成多窗口涨跌幅宽表。

    每个板块只请求一次 K 线，长度为 ``max(windows) + 10``。

    Args:
        client: 已连接的行情客户端（``MacClient`` 或其替身）。
        board_type: 板块类型（HY / GN / FG / DQ / ALL）。
        windows: 回溯交易日数序列。
        target_date: 截止日期 ``YYYYMMDD``，``None`` 表示最新交易日。
        progress: 是否在 stderr 上显示进度条。

    Returns:
        DataFrame，列为 ``code, name, close_end`` 加各窗口 ``chg_{w}d``。
    """
    columns = [*BASE_COLUMNS, *change_pct_columns(windows)]
    boards_df = client.get_board_list(board_type)
    if boards_df.empty:
        return pd.DataFrame(columns=columns)

    fetch_count = max(windows) + _KLINE_BUFFER
    target_ts = _target_timestamp(target_date)
    board_rows = [row for _, row in boards_df.iterrows()]

    rows: list[dict[str, Any]] = []
    with _board_progress(board_rows, enabled=progress) as tracked:
        for row in tracked:
            board_code = str(row["code"])
            board_market = int(row["market"]) if "market" in row.index else 1
            try:
                kline_df = client.get_stock_kline(
                    market=board_market,
                    code=board_code,
                    period=Period.DAILY,
                    count=fetch_count,
                    adjust=Adjust.NONE,
                )
            except Exception:  # noqa: BLE001 — 单个板块失败不应中断整轮扫描
                continue

            changes = compute_window_changes(kline_df, windows, target_ts)
            if changes is None:
                continue
            rows.append({"code": board_code, "name": str(row.get("name", "")), **changes})

    return pd.DataFrame(rows, columns=columns)


def add_window_ranks(
    change_df: pd.DataFrame,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    *,
    ascending: bool = False,
) -> pd.DataFrame:
    """为宽表补上每个窗口的名次列 ``rank_{w}d``（1 = 涨幅最大）。

    Args:
        change_df: :func:`fetch_board_change_matrix` 输出的宽表。
        windows: 回溯交易日数序列。
        ascending: ``True`` 时 1 = 跌幅最大（找共振最弱板块）。

    Returns:
        新的 DataFrame（不修改入参）。并列名次取相同的较小值。
    """
    ranked = change_df.copy()
    for window in windows:
        values = pd.to_numeric(ranked[f"chg_{window}d"], errors="coerce")
        ranked[f"rank_{window}d"] = values.rank(ascending=ascending, method="min").astype("Int64")
    return ranked


def select_consensus(
    ranked_df: pd.DataFrame,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    top_n: int = 10,
) -> pd.DataFrame:
    """筛出在**全部**窗口中都进入 Top-N 的板块。

    Args:
        ranked_df: :func:`add_window_ranks` 输出的宽表。
        windows: 回溯交易日数序列。
        top_n: 每个窗口的入选名次门槛。

    Returns:
        交集结果，按 ``avg_rank`` 升序、最长窗口涨幅降序排列；
        列顺序为 ``code, name, close_end, chg/rank 交替, avg_rank``。
    """
    cols = rank_columns(windows)
    ordered_cols = [
        *BASE_COLUMNS,
        *[c for w in windows for c in (f"chg_{w}d", f"rank_{w}d")],
        "avg_rank",
    ]
    if ranked_df.empty:
        return pd.DataFrame(columns=ordered_cols)

    ranks = ranked_df[cols].astype("Float64")
    mask = (ranks.notna().all(axis=1) & ranks.le(top_n).all(axis=1)).fillna(False).astype(bool)

    selected = ranked_df.loc[mask].copy()
    if selected.empty:
        return pd.DataFrame(columns=ordered_cols)

    selected["avg_rank"] = ranks.loc[mask].mean(axis=1).astype("float64").round(2)
    selected[cols] = selected[cols].astype("int64")
    return (
        selected.sort_values(["avg_rank", f"chg_{max(windows)}d"], ascending=[True, False])
        .reset_index(drop=True)
        .reindex(columns=ordered_cols)
    )


def build_consensus(
    client: BoardKlineSource,
    board_type: BoardType,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    top_n: int = 10,
    target_date: int | None = None,
    *,
    ascending: bool = False,
    progress: bool = False,
) -> pd.DataFrame:
    """抓取 → 排名 → 求交集的完整流程。"""
    change_df = fetch_board_change_matrix(
        client, board_type, windows, target_date, progress=progress
    )
    ranked_df = add_window_ranks(change_df, windows, ascending=ascending)
    return select_consensus(ranked_df, windows, top_n)


class _NullProgress:
    """非 tty 或未启用进度时的空壳上下文。"""

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = items

    def __enter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __exit__(self, *_: object) -> None:
        return None


def _board_progress(items: Sequence[Any], *, enabled: bool) -> Any:
    """板块遍历进度条；输出到 stderr 以免污染 stdout 的 JSON/CSV。"""
    if not enabled:
        return _NullProgress(items)
    return click.progressbar(items, label="扫描板块", file=sys.stderr)


@click.command("board-change-consensus")
@click.option("--type", "board_type", default="HY", help="板块类型: HY/GN/FG/DQ/ALL")
@click.option("--days", "days_raw", default="3,5,10", help="回溯交易日数，逗号分隔 (默认 3,5,10)")
@click.option("--top", "top_n", default=10, type=int, help="每个窗口的 Top-N 门槛 (默认 10)")
@click.option("--date", "target_date", default=None, type=int, help="截止日期 YYYYMMDD (默认最新)")
@click.option("--asc", is_flag=True, help="改取跌幅共振榜 (默认涨幅)")
@click.option("--names-only", "names_only", is_flag=True, help="只输出板块名称，每行一个")
@click.option("--table", "use_table", is_flag=True, help="表格输出")
@click.option("--output", "output_fmt", type=click.Choice(["json", "table", "csv"]), default="json")
@click.option("--progress/--no-progress", default=True, help="stderr 进度条 (默认开)")
def main(
    board_type: str,
    days_raw: str,
    top_n: int,
    target_date: int | None,
    asc: bool,
    names_only: bool,
    use_table: bool,
    output_fmt: str,
    progress: bool,
) -> None:
    """板块多周期共振榜：同时进入各窗口 Top-N 的板块。

    示例：

      python board_change_consensus.py --type HY --days 3,5,10 --top 10 --table

      python board_change_consensus.py --type GN --top 15 --names-only

      python board_change_consensus.py --type HY --date 20250530 --output csv
    """
    if top_n < 1:
        raise click.UsageError(f"--top 必须 >= 1，got {top_n}")

    try:
        windows = parse_windows(days_raw)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    board_type_enum = parse_board_type(board_type)

    with get_mac_client() as client:
        result = build_consensus(
            client,
            board_type_enum,
            windows,
            top_n,
            target_date,
            ascending=asc,
            progress=progress,
        )

    if result.empty:
        window_desc = "/".join(str(w) for w in windows)
        click.echo(f"无共振板块：{window_desc} 日榜单 Top-{top_n} 无交集。", err=True)

    if names_only:
        if not result.empty:
            click.echo("\n".join(result["name"].astype(str)))
        return

    print_output(result, "table" if use_table else output_fmt)


if __name__ == "__main__":
    main()
