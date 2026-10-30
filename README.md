# tdxleigh

板块量化小工具集，基于 **[easy-tdx](https://github.com/handsomejustin/easy-tdx) 1.32.7**（导入名 `easy_tdx`，CLI `easy-tdx`）的 MAC 通达信行情。

| 文件 | 作用 |
|------|------|
| `board_change_consensus.py` | 多周期涨幅榜 Top-N **交集**（共振板块） |
| `board_etf_match.py` | 为共振/指定板块匹配高相关、流动性好的场内 ETF |
| `cli_output.py` | `--table` 输出（CJK 对齐；JSON/CSV 仍走 `easy_tdx`） |
| `test/` | pytest 单元测试（替身客户端，无需联网） |

典型流程：先跑 **共振榜** → 再对板块跑 **ETF 匹配**。

## 环境（uv）

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+（`requires-python = >=3.12,<3.14`）。

**easy-tdx 1.32.7** 与 conda 一致，但 **不在 pypi.org**；`pip` 若配置了清华/阿里镜像能直接装。本项目在 `vendor/wheels/`  vendoring 同版本 wheel，并在 `pyproject.toml` 用 `[tool.uv.sources]` 指向它；其余包默认走清华镜像。`easy-tdx` 要求 **pandas 2.x**（已锁定 `<3`）。

```bash
cd tdxleigh
uv sync --all-groups
uv run easy-tdx --help
uv run python board_change_consensus.py --help
uv run python board_etf_match.py --help
```

Nu 壳别名示例（见 `config.nu`）：`tdxe` → `uv run … easy-tdx`，`tdxp` → `uv run … python`。

---

## `board_change_consensus.py` — 板块共振榜

### 做什么

在指定板块类型（行业 HY、概念 GN 等）下，对每个板块只拉一次日 K（长度 ≈ `max(days) + 缓冲`），一次算出各窗口涨跌幅，再筛出在**每一个**窗口都进入 Top-N 的板块。

等价于多次运行 `easy-tdx board-change-ranking` 后人工找交集，但通常快约 3 倍（少重复拉 K 线）。

### 用法

```bash
uv run python board_change_consensus.py --type HY --days 3,5,10 --top 10 --table
uv run python board_change_consensus.py --type GN --days 3,5 --top 5 --table
uv run python board_change_consensus.py --type HY --days 3,5,7,13 --top 10 --table
uv run python board_change_consensus.py --type GN --top 15 --names-only
uv run python board_change_consensus.py --type HY --date 20250530 --output csv
uv run python board_change_consensus.py --type HY --asc --table          # 跌幅共振
```

常用选项：

| 选项 | 说明 |
|------|------|
| `--type` | `HY` / `GN` / `FG` / `DQ` / `ALL`（默认 `HY`） |
| `--days` | 逗号分隔的交易日窗口，如 `3,5,10`（默认 `3,5,10`） |
| `--top` | 每个窗口的名次门槛（默认 `10`） |
| `--date` | 截止日 `YYYYMMDD`（默认最新） |
| `--table` / `--output` | 表格 / `json` / `csv`（默认 JSON） |
| `--names-only` | 只打印板块名称 |
| `--progress` / `--no-progress` | stderr 扫描进度条 |

### 输出列

`code`, `name`, `close_end`，随后各窗口成对出现 `chg_{w}d`, `rank_{w}d`，最后一列 `avg_rank`（各窗口名次均值，越小越强）。

---

## `board_etf_match.py` — 板块 → ETF

### 匹配方法

1. ETF 全集：MAC 伪板块 `11005`，按成交额过滤，剔除债券/货币/跨境/商品，默认剔除宽基。
2. 短名单：全体 ETF 与板块指数做 60 日日收益回归，取 R² 前 30 + 名称命中板块关键词者。
3. 日内回归：短名单与板块指数做 20 日 30 分钟收益回归（R²、β）。
4. 打分：`0.5·R²_intraday + 0.2·R²_1d + 0.15·name_hit + 0.15·liquidity_pct`；同主题只留最高分。

通达信协议不提供 ETF 持仓/跟踪指数，以收益相关性代替持仓重合度。

### 用法

板块代码写在**所有选项之后**（方便 Nu 脚本把 ticker 放末尾）：

```bash
uv run python board_etf_match.py --table                           # 先跑共振榜，再逐板块匹配
uv run python board_etf_match.py --table 881289
uv run python board_etf_match.py --keywords 卫星,军工 --tickers-only 881289
uv run python board_etf_match.py --freq 5 --intraday-days 10 881289
uv run python board_etf_match.py --include-broad --keep-duplicates 881289
uv run python board_etf_match.py --tickers-only 881289 881007
```

### 输出列

`board_name`, `rank`, `code`, `name`, `score`, `r2_intraday`, `beta_intraday`, `r2_1d`, `beta_1d`, `chg_5d`, `amount_mn`（成交额，百万元）。

---

## 测试

```bash
uv run pytest
```
