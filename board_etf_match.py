"""板块 → 高相关 ETF 匹配：为共振板块找出可用于中长线布局的 ETF。

流程：

1. 取全部场内 ETF 报价（MAC 协议伪板块 ``11005``），按成交额过滤，
   剔除债券/货币/跨境/商品类 ETF。
2. 全体候选 ETF 与板块指数做日线收益回归，取 R² 最高的若干只，
   并强制纳入名称命中板块关键词的 ETF，组成短名单。
3. 短名单 ETF 与板块指数做日内（默认 30 分钟）收益回归。
4. 综合打分::

       score = w_intraday·R²_intraday + w_daily·R²_1d + w_name·name_hit + w_liq·liquidity_pct

   同一主题（如多家“卫星ETF”）默认只保留得分最高的一只。

通达信协议不提供 ETF 持仓与跟踪指数，因此用收益相关性代替持仓重合度。

用法::

    python board_etf_match.py --table                       # 先跑共振榜，再逐板块匹配 ETF
    python board_etf_match.py --table 881289                # 指定板块（代码放末尾）
    python board_etf_match.py --tickers-only 881289 881007
    python board_etf_match.py --keywords 卫星,军工 --freq 5 881289
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import click
import numpy as np
import pandas as pd

ETF_BOARD_SYMBOL: Final[str] = "11005"
BOARD_MARKET: Final[int] = 1
ETF_LIST_COUNT: Final[int] = 5000

EXCLUDED_CODE_PREFIXES: Final[tuple[str, ...]] = ("511", "513", "518")
EXCLUDED_NAME_KEYWORDS: Final[tuple[str, ...]] = (
    "债", "货币", "现金", "短融", "理财", "黄金", "白银", "豆粕", "原油", "油气LOF",
    "有色期货", "能源化工", "纳指", "纳斯达克", "标普", "道琼斯", "恒生", "港股", "香港",
    "H股", "中概", "日经", "东证", "德国", "法国", "美国", "沙特", "亚太", "东南亚",
    "全球", "海外", "QDII", "巴西", "新加坡",
)
BROAD_MARKET_KEYWORDS: Final[tuple[str, ...]] = (
    "沪深300", "300ETF", "中证500", "500ETF", "中证800", "中证1000", "1000ETF", "中证2000",
    "2000ETF", "A500", "A50", "上证", "深证", "创业板", "科创50", "科创100", "科创200",
    "科创综指", "科创板", "双创", "中小100", "深100", "全指", "综指", "MSCI", "自由现金流",
)
GENERIC_BIGRAMS: Final[frozenset[str]] = frozenset(
    {"行业", "板块", "设备", "服务", "制造", "产品", "其他", "综合", "概念", "指数"}
)

INTRADAY_FREQS: Final[tuple[int, ...]] = (5, 15, 30, 60)
BARS_PER_DAY_MINUTES: Final[int] = 240

OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    "board_name", "rank", "code", "name", "score",
    "r2_intraday", "beta_intraday", "r2_1d", "beta_1d", "chg_5d", "amount_mn",
)


class KlineSource(Protocol):
    """``MacClient`` 中本脚本用到的最小接口（便于测试替身注入）。"""

    def get_board_members(self, board_symbol: str, count: int = ...) -> pd.DataFrame: ...

    def get_stock_kline(
        self,
        *,
        market: int,
        code: str,
        period: Any,
        count: int,
        adjust: Any,
    ) -> pd.DataFrame: ...


@dataclass(frozen=True, slots=True)
class KlineSpec:
    """一次 K 线请求的周期 / 条数 / 复权方式（period、adjust 为 easy_tdx 枚举）。"""

    period: Any
    count: int
    adjust: Any


@dataclass(frozen=True, slots=True)
class KlinePlan:
    board_daily: KlineSpec
    board_intraday: KlineSpec
    etf_daily: KlineSpec
    etf_intraday: KlineSpec


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    intraday: float = 0.5
    daily: float = 0.2
    name: float = 0.15
    liquidity: float = 0.15


@dataclass(frozen=True, slots=True)
class MatchConfig:
    top_n: int = 10
    min_score: float = 0.47
    shortlist: int = 30
    min_obs_daily: int = 30
    min_obs_intraday: int = 60
    chg_days: int = 5
    one_per_theme: bool = True
    weights: ScoreWeights = field(default_factory=ScoreWeights)


# --------------------------------------------------------------------------- #
# 纯计算（无网络）
# --------------------------------------------------------------------------- #


def filter_etf_universe(
    etf_df: pd.DataFrame,
    min_amount: float,
    exclude_keywords: Sequence[str] = EXCLUDED_NAME_KEYWORDS,
    exclude_prefixes: Sequence[str] = EXCLUDED_CODE_PREFIXES,
) -> pd.DataFrame:
    """过滤 ETF 报价表，返回 ``market, code, name, amount, liquidity_pct``。

    ``liquidity_pct`` 为成交额在过滤后全集中的百分位（0~1]。
    """
    columns = ["market", "code", "name", "amount", "liquidity_pct"]
    if etf_df.empty:
        return pd.DataFrame(columns=columns)

    codes = etf_df["code"].astype(str).str.zfill(6)
    names = etf_df["name"].astype(str)
    amount = pd.to_numeric(etf_df["amount"], errors="coerce").fillna(0.0)

    mask = (amount >= min_amount) & ~codes.str.startswith(tuple(exclude_prefixes))
    if exclude_keywords:
        pattern = "|".join(re.escape(k) for k in exclude_keywords)
        mask &= ~names.str.contains(pattern, regex=True)

    universe = pd.DataFrame(
        {
            "market": etf_df["market"].astype("int64"),
            "code": codes,
            "name": names,
            "amount": amount,
        }
    ).loc[mask]
    universe = universe.drop_duplicates("code").reset_index(drop=True)
    universe["liquidity_pct"] = universe["amount"].rank(pct=True)
    return universe.reindex(columns=columns)


def board_keywords(board_name: str, extra: Sequence[str] = ()) -> tuple[str, ...]:
    """板块名称拆成二元组关键词（去掉泛化词），并拼接用户追加关键词。"""
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", board_name)
    grams = [cleaned[i : i + 2] for i in range(len(cleaned) - 1)] or ([cleaned] if cleaned else [])
    words = [*(e.strip() for e in extra), *(g for g in grams if g not in GENERIC_BIGRAMS)]
    return tuple(dict.fromkeys(w for w in words if w))


def name_hits(names: pd.Series, keywords: Sequence[str]) -> pd.Series:
    """名称命中任一关键词记 1.0，否则 0.0。"""
    if not keywords:
        return pd.Series(0.0, index=names.index)
    pattern = "|".join(re.escape(k) for k in keywords)
    return names.astype(str).str.contains(pattern, regex=True).astype("float64")


def extract_theme(names: pd.Series) -> pd.Series:
    """``卫星ETF富国`` → ``卫星ETF``；用于同主题去重。"""
    theme = names.astype(str).str.extract(r"^(.*?ETF)", expand=False)
    return theme.fillna(names.astype(str))


def close_series(kline_df: pd.DataFrame) -> pd.Series:
    """K 线 DataFrame → 以 datetime 为索引的收盘价序列。"""
    if kline_df.empty:
        return pd.Series(dtype="float64")
    ordered = (
        kline_df.assign(datetime=pd.to_datetime(kline_df["datetime"]))
        .drop_duplicates("datetime", keep="last")
        .set_index("datetime")
        .sort_index()
    )
    return ordered["close"].astype("float64")


def build_close_panel(closes: Mapping[str, pd.Series]) -> pd.DataFrame:
    """``{code: close_series}`` → 宽表（行 = datetime，列 = code）。"""
    if not closes:
        return pd.DataFrame(dtype="float64")
    series: dict[str, pd.Series[Any]] = dict(closes)
    return pd.concat(series, axis="columns", sort=True)


def to_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change(fill_method=None).iloc[1:]


def regression_stats(
    asset_returns: pd.DataFrame,
    ref_returns: pd.Series,
    min_obs: int,
) -> pd.DataFrame:
    """各列收益对参考收益的成对（剔除缺失）相关系数、R²、β 与样本数。

    样本数不足 ``min_obs`` 的列统计量置为 NaN。
    """
    columns = ["corr", "r2", "beta", "n_obs"]
    if asset_returns.empty:
        return pd.DataFrame(columns=columns, dtype="float64")

    ref = ref_returns.reindex(asset_returns.index)
    valid = asset_returns.notna() & ref.notna().to_numpy()[:, None]
    x = asset_returns.where(valid)
    b = pd.DataFrame(
        np.broadcast_to(ref.to_numpy(dtype="float64")[:, None], x.shape),
        index=x.index,
        columns=x.columns,
    ).where(valid)

    n_obs = valid.sum()
    dx = x - x.mean()
    db = b - b.mean()
    dof = (n_obs - 1).where(n_obs > 1)
    cov = (dx * db).sum() / dof
    var_b = (db**2).sum() / dof
    var_x = (dx**2).sum() / dof

    corr = (cov / np.sqrt(var_b * var_x)).replace([np.inf, -np.inf], np.nan)
    beta = (cov / var_b).replace([np.inf, -np.inf], np.nan)
    stats = pd.DataFrame({"corr": corr, "r2": corr**2, "beta": beta, "n_obs": n_obs})
    stats.loc[n_obs < min_obs, ["corr", "r2", "beta"]] = np.nan
    return stats


def window_change_pct(close_panel: pd.DataFrame, window: int) -> pd.Series:
    """各列最近 ``window`` 根 K 线的涨跌幅（%）。"""
    if len(close_panel) <= window:
        return pd.Series(np.nan, index=close_panel.columns, dtype="float64")
    filled = close_panel.ffill()
    return ((filled.iloc[-1] / filled.iloc[-1 - window] - 1.0) * 100).round(2)


def shortlist_etfs(
    universe: pd.DataFrame,
    etf_daily_closes: pd.DataFrame,
    board_daily_close: pd.Series,
    keywords: Sequence[str],
    config: MatchConfig,
) -> pd.DataFrame:
    """日线 R² 前 ``config.shortlist`` 名 ∪ 名称命中者（均要求正相关）。

    返回 universe 子集，附加 ``r2_1d, beta_1d, corr_1d, name_hit`` 列。
    """
    if universe.empty or etf_daily_closes.empty or board_daily_close.empty:
        return universe.iloc[0:0].assign(r2_1d=[], beta_1d=[], corr_1d=[], name_hit=[])

    stats = regression_stats(
        to_returns(etf_daily_closes),
        board_daily_close.pct_change(fill_method=None).iloc[1:],
        config.min_obs_daily,
    )
    enriched = universe.assign(
        r2_1d=universe["code"].map(stats["r2"]),
        beta_1d=universe["code"].map(stats["beta"]),
        corr_1d=universe["code"].map(stats["corr"]),
        name_hit=name_hits(universe["name"], keywords),
    )
    positive = enriched["corr_1d"] > 0
    top_daily = enriched.loc[positive].nlargest(config.shortlist, "r2_1d").index
    by_name = enriched.index[positive & (enriched["name_hit"] > 0)]
    return enriched.loc[top_daily.union(by_name)].reset_index(drop=True)


def score_candidates(features: pd.DataFrame, weights: ScoreWeights) -> pd.Series:
    """加权得分；缺失的 R² 记 0。"""
    filled = features[["r2_intraday", "r2_1d", "name_hit", "liquidity_pct"]].fillna(0.0)
    score = (
        weights.intraday * filled["r2_intraday"]
        + weights.daily * filled["r2_1d"]
        + weights.name * filled["name_hit"]
        + weights.liquidity * filled["liquidity_pct"]
    )
    return score.round(4)


def rank_shortlist(
    shortlist: pd.DataFrame,
    etf_intraday_closes: pd.DataFrame,
    board_intraday_close: pd.Series,
    etf_daily_closes: pd.DataFrame,
    config: MatchConfig,
) -> pd.DataFrame:
    """对短名单做日内回归并打分，返回得分 > ``config.min_score`` 且按得分降序的前 ``config.top_n`` 只。"""
    result_cols = [c for c in OUTPUT_COLUMNS if c != "board_name"]
    if shortlist.empty:
        return pd.DataFrame(columns=result_cols)

    intraday = regression_stats(
        to_returns(etf_intraday_closes),
        board_intraday_close.pct_change(fill_method=None).iloc[1:],
        config.min_obs_intraday,
    )
    chg = window_change_pct(etf_daily_closes, config.chg_days)

    features = shortlist.assign(
        r2_intraday=shortlist["code"].map(intraday["r2"]),
        beta_intraday=shortlist["code"].map(intraday["beta"]),
        corr_intraday=shortlist["code"].map(intraday["corr"]),
        chg_5d=shortlist["code"].map(chg),
        theme=extract_theme(shortlist["name"]),
    )
    features = features.loc[~(features["corr_intraday"] <= 0)]
    features = features.assign(score=score_candidates(features, config.weights))
    features = features.loc[features["score"] > config.min_score]
    features = features.sort_values(["score", "amount"], ascending=[False, False])
    if config.one_per_theme:
        features = features.drop_duplicates("theme", keep="first")

    top = features.head(config.top_n).reset_index(drop=True)
    top["rank"] = np.arange(1, len(top) + 1)
    top["amount_mn"] = (top["amount"] / 1e6).round(1)
    for col in ("r2_intraday", "beta_intraday", "r2_1d", "beta_1d"):
        top[col] = top[col].round(3)
    return top.reindex(columns=result_cols)


# --------------------------------------------------------------------------- #
# 网络抓取（同步客户端 + asyncio 线程并发）
# --------------------------------------------------------------------------- #


def _fetch_close(client: KlineSource, market: int, code: str, spec: KlineSpec) -> pd.Series:
    try:
        kline_df = client.get_stock_kline(
            market=market, code=code, period=spec.period, count=spec.count, adjust=spec.adjust
        )
    except Exception:  # noqa: BLE001 — 单只标的失败不应中断整轮扫描
        return pd.Series(dtype="float64")
    return close_series(kline_df)


async def fetch_close_panel(
    clients: Sequence[KlineSource],
    securities: pd.DataFrame,
    spec: KlineSpec,
    *,
    label: str = "",
    progress: bool = False,
) -> pd.DataFrame:
    """并发拉取 ``securities``（market, code）的收盘价宽表；每个连接一个 worker。"""
    if securities.empty or not clients:
        return pd.DataFrame(dtype="float64")

    queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
    for market, code in zip(securities["market"].astype(int), securities["code"].astype(str)):
        queue.put_nowait((int(market), code))

    total = queue.qsize()
    closes: dict[str, pd.Series] = {}
    done = 0

    async def worker(client: KlineSource) -> None:
        nonlocal done
        while not queue.empty():
            market, code = queue.get_nowait()
            series = await asyncio.to_thread(_fetch_close, client, market, code, spec)
            done += 1
            if not series.empty:
                closes[code] = series
            if progress and (done % 25 == 0 or done == total):
                click.echo(f"\r{label} {done}/{total}", nl=False, err=True)

    await asyncio.gather(*(worker(c) for c in clients))
    if progress:
        click.echo("", err=True)
    return build_close_panel(closes)


async def fetch_board_close(client: KlineSource, code: str, spec: KlineSpec) -> pd.Series:
    return await asyncio.to_thread(_fetch_close, client, BOARD_MARKET, code, spec)


async def match_boards_to_etfs(
    clients: Sequence[KlineSource],
    boards: pd.DataFrame,
    plan: KlinePlan,
    *,
    min_amount: float,
    extra_keywords: Sequence[str] = (),
    exclude_keywords: Sequence[str] = (*EXCLUDED_NAME_KEYWORDS, *BROAD_MARKET_KEYWORDS),
    config: MatchConfig = MatchConfig(),
    progress: bool = False,
) -> pd.DataFrame:
    """完整流程：ETF 全集 → 日线短名单 → 日内回归打分，逐板块输出。

    Args:
        clients: 已连接的行情客户端（每个对应一个并发 worker）。
        boards: 至少含 ``code, name`` 列的板块表（如共振榜结果）。
        plan: 板块 / ETF 的日线与日内 K 线请求参数。
        min_amount: ETF 当日成交额下限（元）。
        extra_keywords: 追加的名称关键词（对所有板块生效）。
        exclude_keywords: 名称含任一关键词的 ETF 不参与匹配（默认剔除宽基）。
        config: 打分与筛选参数。
        progress: 是否在 stderr 输出进度。
    """
    empty = pd.DataFrame(columns=list(OUTPUT_COLUMNS))
    if boards.empty or not clients:
        return empty

    etf_raw = await asyncio.to_thread(
        clients[0].get_board_members, ETF_BOARD_SYMBOL, ETF_LIST_COUNT
    )
    universe = filter_etf_universe(etf_raw, min_amount, exclude_keywords)
    if universe.empty:
        return empty

    daily_panel = await fetch_close_panel(
        clients, universe, plan.etf_daily, label="ETF 日线", progress=progress
    )
    intraday_cache: dict[str, pd.Series] = {}
    results: list[pd.DataFrame] = []

    for board_code, board_name in zip(boards["code"].astype(str), boards["name"].astype(str)):
        board_daily = await fetch_board_close(clients[0], board_code, plan.board_daily)
        if board_daily.empty:
            continue

        keywords = board_keywords(board_name, extra_keywords)
        shortlist = shortlist_etfs(universe, daily_panel, board_daily, keywords, config)
        if shortlist.empty:
            continue

        missing = shortlist.loc[~shortlist["code"].isin(intraday_cache.keys())]
        fresh = await fetch_close_panel(
            clients, missing, plan.etf_intraday, label=f"{board_name} 日内", progress=progress
        )
        intraday_cache.update({str(c): fresh[c].dropna() for c in fresh.columns})

        board_intraday = await fetch_board_close(clients[0], board_code, plan.board_intraday)
        intraday_panel = build_close_panel(
            {c: intraday_cache[c] for c in shortlist["code"] if c in intraday_cache}
        )
        ranked = rank_shortlist(shortlist, intraday_panel, board_intraday, daily_panel, config)
        if not ranked.empty:
            results.append(ranked.assign(board_name=board_name))

    if not results:
        return empty
    return pd.concat(results, ignore_index=True).reindex(columns=list(OUTPUT_COLUMNS))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@contextmanager
def open_client_pool(size: int) -> Iterator[list[KlineSource]]:
    """建立 ``size`` 个 MAC 连接，退出时全部关闭。

    ``from_best_host`` 会写 ``~/.easy_tdx/config.json``，并发调用在 Windows 上
    触发文件占用错误，因此只测速一次，其余连接复用已保存的最佳主机。
    """
    from easy_tdx.cli.conn import get_mac_client
    from easy_tdx.mac.client import MacClient

    with ExitStack() as stack:
        clients: list[KlineSource] = [stack.enter_context(get_mac_client())]
        extra = [MacClient() for _ in range(size - 1)]
        for client in extra:
            stack.callback(client.close)
        if extra:
            with ThreadPoolExecutor(max_workers=len(extra)) as pool:
                list(pool.map(MacClient.connect, extra))
        clients.extend(extra)
        yield clients


def build_kline_plan(daily_days: int, intraday_days: int, freq: int) -> KlinePlan:
    from easy_tdx.mac.enums import Adjust, Period

    intraday_period = {5: Period.MIN_5, 15: Period.MIN_15, 30: Period.MIN_30, 60: Period.MIN_60}[freq]
    daily_count = daily_days + 1
    intraday_count = intraday_days * BARS_PER_DAY_MINUTES // freq + 1
    return KlinePlan(
        board_daily=KlineSpec(Period.DAILY, daily_count, Adjust.NONE),
        board_intraday=KlineSpec(intraday_period, intraday_count, Adjust.NONE),
        etf_daily=KlineSpec(Period.DAILY, daily_count, Adjust.QFQ),
        etf_intraday=KlineSpec(intraday_period, intraday_count, Adjust.QFQ),
    )


def combine_board_codes(boards_raw: str, board_codes: Sequence[str]) -> str:
    """合并 ``--boards``（已弃用）与末尾 positional 板块代码，去重保序。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for chunk in (boards_raw, ",".join(board_codes)):
        for code in chunk.replace(" ", ",").split(","):
            c = code.strip()
            if c and c not in seen:
                seen.add(c)
                ordered.append(c)
    return ",".join(ordered)


def resolve_boards(raw: str, consensus_args: tuple[str, str, int], progress: bool) -> pd.DataFrame:
    """板块代码非空时查名称；否则先跑共振榜。返回 ``code, name``。"""
    from easy_tdx.cli.conn import get_mac_client
    from easy_tdx.mac.enums import BoardType

    if raw.strip():
        codes = [c.strip() for c in raw.replace(" ", ",").split(",") if c.strip()]
        with get_mac_client() as client:
            board_list = client.get_board_list(BoardType.ALL)
        names = dict(zip(board_list["code"].astype(str), board_list["name"].astype(str)))
        return pd.DataFrame({"code": codes, "name": [names.get(c, "") for c in codes]})

    from board_change_consensus import build_consensus, parse_windows
    from easy_tdx.cli.parsers import parse_board_type

    board_type, days_raw, top_n = consensus_args
    with get_mac_client() as client:
        consensus = build_consensus(
            client, parse_board_type(board_type), parse_windows(days_raw), top_n, progress=progress
        )
    return consensus.reindex(columns=["code", "name"])


@click.command("board-etf-match")
@click.option(
    "--boards",
    "boards_raw",
    default="",
    hidden=True,
    help="已弃用：请把板块代码写在命令末尾",
)
@click.option("--type", "board_type", default="HY", help="共振榜板块类型: HY/GN/FG/DQ/ALL")
@click.option("--days", "days_raw", default="3,5,10", help="共振榜回溯交易日数")
@click.option("--top", "board_top", default=10, type=int, help="共振榜每窗口 Top-N 门槛")
@click.option("--keywords", "keywords_raw", default="", help="追加 ETF 名称关键词，逗号分隔")
@click.option("--etf-top", default=10, type=int, help="每个板块输出 ETF 数 (默认 10)")
@click.option("--min-score", default=0.47, type=float, help="ETF 得分下限，仅保留 score > 此值 (默认 0.47)")
@click.option("--shortlist", default=30, type=int, help="日线 R² 短名单长度 (默认 30)")
@click.option("--min-amount", default=5e7, type=float, help="ETF 当日成交额下限，元 (默认 5e7)")
@click.option("--daily-days", default=60, type=int, help="日线回归窗口，交易日 (默认 60)")
@click.option("--intraday-days", default=20, type=int, help="日内回归窗口，交易日 (默认 20)")
@click.option("--freq", default=30, type=click.Choice([str(f) for f in INTRADAY_FREQS]), help="日内周期，分钟")
@click.option("--keep-duplicates", is_flag=True, help="保留同主题的多只 ETF")
@click.option("--include-broad", is_flag=True, help="保留宽基 ETF（沪深300/中证1000/A500 等）")
@click.option("--connections", default=4, type=int, help="并发连接数 (默认 4)")
@click.option("--tickers-only", is_flag=True, help="每个板块一行：板块代码 名称: ETF 代码列表")
@click.option("--table", "use_table", is_flag=True, help="表格输出")
@click.option("--output", "output_fmt", type=click.Choice(["json", "table", "csv"]), default="json")
@click.option("--progress/--no-progress", default=True, help="stderr 进度 (默认开)")
@click.argument("board_codes", nargs=-1)
def main(
    boards_raw: str,
    board_type: str,
    days_raw: str,
    board_top: int,
    keywords_raw: str,
    etf_top: int,
    min_score: float,
    shortlist: int,
    min_amount: float,
    daily_days: int,
    intraday_days: int,
    freq: str,
    keep_duplicates: bool,
    include_broad: bool,
    connections: int,
    tickers_only: bool,
    use_table: bool,
    output_fmt: str,
    progress: bool,
    board_codes: tuple[str, ...],
) -> None:
    """为板块匹配高相关 ETF。

    板块代码写在**所有选项之后**（可多个，如 ``881289 881007``）；省略则先跑共振榜。

    示例：

      python board_etf_match.py --table

      python board_etf_match.py --keywords 卫星,军工 --table 881289
    """
    if min(etf_top, shortlist, connections, daily_days, intraday_days, board_top) < 1:
        raise click.UsageError("--etf-top/--shortlist/--connections/--*-days/--top 必须 >= 1")

    boards_spec = combine_board_codes(boards_raw, board_codes)
    boards = resolve_boards(boards_spec, (board_type, days_raw, board_top), progress)
    if boards.empty:
        click.echo("没有可匹配的板块。", err=True)
        return

    config = MatchConfig(
        top_n=etf_top,
        min_score=min_score,
        shortlist=shortlist,
        min_obs_daily=max(2, int(daily_days * 0.5)),
        min_obs_intraday=max(2, int(intraday_days * BARS_PER_DAY_MINUTES // int(freq) * 0.5)),
        one_per_theme=not keep_duplicates,
    )
    extra = tuple(k.strip() for k in keywords_raw.split(",") if k.strip())
    plan = build_kline_plan(daily_days, intraday_days, int(freq))
    excluded = EXCLUDED_NAME_KEYWORDS if include_broad else (*EXCLUDED_NAME_KEYWORDS, *BROAD_MARKET_KEYWORDS)

    with open_client_pool(connections) as clients:
        result = asyncio.run(
            match_boards_to_etfs(
                clients,
                boards,
                plan,
                min_amount=min_amount,
                extra_keywords=extra,
                exclude_keywords=excluded,
                config=config,
                progress=progress,
            )
        )

    if result.empty:
        click.echo("未找到满足条件的 ETF。", err=True)
        return

    if tickers_only:
        for name, group in result.groupby("board_name", sort=False):
            click.echo(f"{name}: {','.join(group['code'].astype(str))}")
        return

    from cli_output import print_output

    print_output(result, "table" if use_table else output_fmt)


if __name__ == "__main__":
    main()
