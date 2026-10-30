from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pandas as pd
import pytest
from board_etf_match import (
    BROAD_MARKET_KEYWORDS,
    ETF_BOARD_SYMBOL,
    EXCLUDED_NAME_KEYWORDS,
    OUTPUT_COLUMNS,
    KlinePlan,
    KlineSpec,
    MatchConfig,
    ScoreWeights,
    board_keywords,
    combine_board_codes,
    extract_theme,
    filter_etf_universe,
    find_boards_by_keywords,
    match_boards_to_etfs,
    name_hits,
    rank_shortlist,
    regression_stats,
    score_candidates,
    shortlist_etfs,
    window_change_pct,
)

BOARD = "881289"


def _etf_quotes() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "market": [0, 1, 1, 1, 0, 1, 1],
            "code": [
                "159206",
                "563230",
                "512660",
                "510300",
                "159941",
                "511990",
                "561530",
            ],
            "name": [
                "卫星ETF永赢",
                "卫星ETF富国",
                "军工ETF国泰",
                "沪深300ETF华泰柏瑞",
                "纳指ETF广发",
                "华宝添益",
                "银行ETF银华",
            ],
            "amount": [9.0e8, 3.4e8, 2.6e8, 5.0e9, 8.0e8, 9.0e9, 4.2e6],
        }
    )


def _price_frames(
    n_daily: int = 61, n_intraday: int = 161
) -> dict[tuple[str, str], pd.DataFrame]:
    """合成价格：卫星 ETF 紧跟板块，军工 ETF 部分跟随，沪深300 与板块无关。"""
    rng = np.random.default_rng(7)
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for kind, n, freq in (("daily", n_daily, "B"), ("intraday", n_intraday, "30min")):
        idx = pd.date_range("2026-07-01", periods=n, freq=freq)
        board_ret = rng.normal(0, 0.02, n)
        noise = {
            BOARD: 0.0,
            "159206": 0.001,
            "563230": 0.002,
            "512660": 0.015,
        }
        for code, sigma in noise.items():
            rets = (
                board_ret * (0.9 if code != BOARD else 1.0) + rng.normal(0, sigma, n)
                if sigma
                else board_ret
            )
            frames[(kind, code)] = pd.DataFrame(
                {"datetime": idx, "close": 100 * np.cumprod(1 + rets)}
            )
        unrelated = rng.normal(0, 0.01, n)
        frames[(kind, "510300")] = pd.DataFrame(
            {"datetime": idx, "close": 4 * np.cumprod(1 + unrelated)}
        )
    return frames


class FakeClient:
    def __init__(self) -> None:
        self.frames = _price_frames()
        self.kline_calls: list[tuple[str, str]] = []

    def get_board_members(self, board_symbol: str, count: int = 0) -> pd.DataFrame:
        assert board_symbol == ETF_BOARD_SYMBOL
        return _etf_quotes()

    def get_stock_kline(
        self, *, market: int, code: str, period: Any, count: int, adjust: Any
    ) -> pd.DataFrame:
        self.kline_calls.append((period, code))
        if code == "999999":
            raise ConnectionError("boom")
        frame = self.frames.get((period, code))
        return (
            pd.DataFrame(columns=["datetime", "close"])
            if frame is None
            else frame.tail(count)
        )


PLAN = KlinePlan(
    board_daily=KlineSpec("daily", 61, "none"),
    board_intraday=KlineSpec("intraday", 161, "none"),
    etf_daily=KlineSpec("daily", 61, "qfq"),
    etf_intraday=KlineSpec("intraday", 161, "qfq"),
)


def test_filter_etf_universe_drops_illiquid_crossborder_and_money() -> None:
    universe = filter_etf_universe(_etf_quotes(), min_amount=5e7)
    assert set(universe["code"]) == {"159206", "563230", "512660", "510300"}
    assert universe["liquidity_pct"].between(0, 1).all()
    assert universe.loc[universe["code"] == "510300", "liquidity_pct"].item() == 1.0


def test_filter_etf_universe_excludes_broad_market() -> None:
    keywords = (*EXCLUDED_NAME_KEYWORDS, *BROAD_MARKET_KEYWORDS)
    universe = filter_etf_universe(
        _etf_quotes(), min_amount=5e7, exclude_keywords=keywords
    )
    assert set(universe["code"]) == {"159206", "563230", "512660"}


def test_filter_etf_universe_empty() -> None:
    assert filter_etf_universe(pd.DataFrame(), 1.0).empty


def test_board_keywords_bigrams_and_extras() -> None:
    assert board_keywords("航天装备", ["卫星", " "]) == ("卫星", "航天", "天装", "装备")
    assert board_keywords("设备") == ()
    assert board_keywords("") == ()


def test_name_hits_and_theme() -> None:
    names = pd.Series(["卫星ETF永赢", "军工ETF国泰", "沪深300ETF"])
    assert name_hits(names, ("卫星", "军工")).tolist() == [1.0, 1.0, 0.0]
    assert name_hits(names, ()).tolist() == [0.0, 0.0, 0.0]
    assert extract_theme(pd.Series(["卫星ETF富国", "华宝添益"])).tolist() == [
        "卫星ETF",
        "华宝添益",
    ]


def test_regression_stats_recovers_beta_and_r2() -> None:
    rng = np.random.default_rng(0)
    ref = pd.Series(rng.normal(0, 0.01, 200))
    assets = pd.DataFrame(
        {"twice": 2 * ref, "flip": -ref, "noise": rng.normal(0, 0.01, 200)}
    )
    assets.loc[:150, "short"] = ref.loc[:150]
    stats = regression_stats(assets, ref, min_obs=160)
    assert stats.loc["twice", "beta"] == pytest.approx(2.0)
    assert stats.loc["twice", "r2"] == pytest.approx(1.0)
    assert stats.loc["flip", "corr"] == pytest.approx(-1.0)
    assert stats.loc["noise", "r2"] < 0.1
    assert np.isnan(stats.loc["short", "r2"])


def test_regression_stats_constant_reference_is_nan() -> None:
    stats = regression_stats(
        pd.DataFrame({"a": [0.01, 0.02, 0.03]}), pd.Series([0.0, 0.0, 0.0]), 2
    )
    assert np.isnan(stats.loc["a", "beta"]) and np.isnan(stats.loc["a", "corr"])


def test_window_change_pct() -> None:
    panel = pd.DataFrame({"a": [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]})
    assert window_change_pct(panel, 5).loc["a"] == pytest.approx(10.0)
    assert window_change_pct(panel.head(3), 5).isna().all()


def test_score_candidates_weights_and_missing() -> None:
    features = pd.DataFrame(
        {
            "r2_intraday": [1.0, np.nan],
            "r2_1d": [1.0, 0.5],
            "name_hit": [1.0, 0.0],
            "liquidity_pct": [1.0, 0.5],
        }
    )
    score = score_candidates(features, ScoreWeights(0.5, 0.2, 0.15, 0.15))
    assert score.tolist() == pytest.approx([1.0, 0.175])


def test_shortlist_and_rank_prefer_tracking_etf() -> None:
    frames = _price_frames()
    universe = filter_etf_universe(_etf_quotes(), 5e7)
    codes = universe["code"].tolist()
    daily = pd.concat(
        {c: frames[("daily", c)].set_index("datetime")["close"] for c in codes}, axis=1
    )
    intraday = pd.concat(
        {c: frames[("intraday", c)].set_index("datetime")["close"] for c in codes},
        axis=1,
    )
    board_daily = frames[("daily", BOARD)].set_index("datetime")["close"]
    board_intraday = frames[("intraday", BOARD)].set_index("datetime")["close"]
    config = MatchConfig(
        top_n=5, min_score=0.0, shortlist=2, min_obs_daily=30, min_obs_intraday=60
    )

    shortlist = shortlist_etfs(universe, daily, board_daily, ("卫星", "军工"), config)
    assert set(shortlist["code"]) == {"159206", "563230", "512660"}

    ranked = rank_shortlist(shortlist, intraday, board_intraday, daily, config)
    assert ranked["code"].tolist()[0] == "159206"
    assert "563230" not in ranked["code"].tolist()  # 同为“卫星ETF”主题，去重
    assert ranked["rank"].tolist() == list(range(1, len(ranked) + 1))
    assert ranked["r2_intraday"].iloc[0] > 0.95

    keep_all = rank_shortlist(
        shortlist,
        intraday,
        board_intraday,
        daily,
        MatchConfig(top_n=5, min_score=0.0, one_per_theme=False),
    )
    assert {"159206", "563230"} <= set(keep_all["code"])

    cutoff = float(keep_all["score"].median())
    filtered = rank_shortlist(
        shortlist,
        intraday,
        board_intraday,
        daily,
        MatchConfig(top_n=5, min_score=cutoff, one_per_theme=False),
    )
    assert (filtered["score"] > cutoff).all()
    assert filtered["rank"].tolist() == list(range(1, len(filtered) + 1))
    assert rank_shortlist(
        shortlist, intraday, board_intraday, daily, MatchConfig(min_score=1.0)
    ).empty


def test_match_boards_to_etfs_end_to_end() -> None:
    client = FakeClient()
    boards = pd.DataFrame({"code": [BOARD, "999999"], "name": ["航天装备", "坏板块"]})
    result = asyncio.run(
        match_boards_to_etfs(
            [client, client],
            boards,
            PLAN,
            min_amount=5e7,
            extra_keywords=("卫星",),
            config=MatchConfig(top_n=3, min_score=0.0, shortlist=2),
        )
    )
    assert list(result.columns) == list(OUTPUT_COLUMNS)
    assert set(result["board_name"]) == {"航天装备"}
    assert result["code"].iloc[0] == "159206"
    assert result["amount_mn"].iloc[0] == pytest.approx(900.0)


def test_find_boards_by_keywords() -> None:
    board_list = pd.DataFrame(
        {
            "code": [880729, "880952", "881289", "880952"],
            "name": ["CXO概念", "卫星导航", "航天装备", "卫星导航"],
        }
    )
    found = find_boards_by_keywords(board_list, ("卫星",))
    assert found.to_dict("list") == {"code": ["880952"], "name": ["卫星导航"]}

    both = find_boards_by_keywords(board_list, ("卫星", "航天"))
    assert both["code"].tolist() == ["880952", "881289"]

    assert find_boards_by_keywords(board_list, ("白酒",)).empty
    assert find_boards_by_keywords(board_list, ()).empty
    assert list(find_boards_by_keywords(pd.DataFrame(), ("卫星",)).columns) == ["code", "name"]


def test_combine_board_codes_trailing_and_legacy() -> None:
    assert combine_board_codes("", ("881289",)) == "881289"
    assert combine_board_codes("", ("881289", "881007")) == "881289,881007"
    assert combine_board_codes("881289", ("881007",)) == "881289,881007"
    assert combine_board_codes("881289", ("881289",)) == "881289"


def test_match_boards_to_etfs_empty_inputs() -> None:
    client = FakeClient()
    assert asyncio.run(
        match_boards_to_etfs([client], pd.DataFrame(), PLAN, min_amount=0)
    ).empty
    boards = pd.DataFrame({"code": [BOARD], "name": ["航天装备"]})
    assert asyncio.run(
        match_boards_to_etfs([client], boards, PLAN, min_amount=1e12)
    ).empty
