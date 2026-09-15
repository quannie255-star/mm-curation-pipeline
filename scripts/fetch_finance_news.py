r"""金融文本采集适配器：akshare 个股新闻 → Sample JSONL（OPS R2）。

运维飞轮的第一段文本进料管道。纪律沿用 fetch_news_corpus.py：
走 akshare 官方接口不自建爬虫、限速、幂等（重跑只补增量）、
解析与失败解耦（单 symbol 失败入清单不阻塞整批）。

行结构（Sample 协议，text_article 模态）：
    {id, text, modality,
     meta: {source, symbol, symbol_name, url, published_at, crawled_at, fetch_run_id}}
id = news_{symbol}_{sha1(url)[:12]}——url 是天然业务键，sha1 截断保 id 短且跨运行稳定。

用法（Windows 加 -X utf8）：
    python -X utf8 scripts/fetch_finance_news.py                     # 内嵌 25 只股票池
    python -X utf8 scripts/fetch_finance_news.py --symbols 600519,000858
    python -X utf8 scripts/fetch_finance_news.py --per-symbol 5      # 冒烟小量

退出码：0 = 至少一个 symbol 采集成功；1 = 全部失败（调度器可见）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

OUT_PATH = REPO / "data/raw/finance_news/news_corpus.jsonl"
PER_SYMBOL_DEFAULT = 20
SLEEP_SECONDS = 1.0

# 与 findata domains/finance/ingest.py 的 UNIVERSE 对齐（2026-09-15 快照，25 只）。
# 零 import 耦合：findata 侧扩池后此处手动同步（周报检查项）。
DEFAULT_UNIVERSE: dict[str, str] = {
    "600519": "贵州茅台",
    "000858": "五粮液",
    "000568": "泸州老窖",
    "300750": "宁德时代",
    "002594": "比亚迪",
    "601012": "隆基绿能",
    "601318": "中国平安",
    "600036": "招商银行",
    "601166": "兴业银行",
    "600030": "中信证券",
    "000333": "美的集团",
    "000651": "格力电器",
    "600887": "伊利股份",
    "600276": "恒瑞医药",
    "300760": "迈瑞医疗",
    "688981": "中芯国际",
    "002371": "北方华创",
    "601899": "紫金矿业",
    "600309": "万华化学",
    "600900": "长江电力",
    "601088": "中国神华",
    "300059": "东方财富",
    "002415": "海康威视",
    "000063": "中兴通讯",
    "002475": "立讯精密",
}


def make_id(symbol: str, url: str) -> str:
    return f"news_{symbol}_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"


def make_row(
    symbol: str,
    symbol_name: str,
    title: str,
    content: str,
    url: str,
    published_at: str,
    fetch_run_id: str,
    crawled_at: str,
    source: str,
) -> dict:
    text = f"{title}\n\n{content}".strip()
    return {
        "id": make_id(symbol, url),
        "text": text,
        "modality": "text_article",
        "meta": {
            "source": source,
            "symbol": symbol,
            "symbol_name": symbol_name,
            "url": url,
            "published_at": published_at,
            "crawled_at": crawled_at,
            "fetch_run_id": fetch_run_id,
        },
    }


def load_seen_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        meta = json.loads(line).get("meta", {})
        if meta.get("url"):
            seen.add(meta["url"])
    return seen


def select_new(rows: list[dict], seen: set[str]) -> list[dict]:
    out = []
    for row in rows:
        url = row["meta"]["url"]
        if url in seen:
            continue
        seen.add(url)
        out.append(row)
    return out


def fetch_symbol_rows(
    symbol: str, symbol_name: str, per_symbol: int, fetch_run_id: str, crawled_at: str
) -> list[dict]:
    """调 akshare 取单只股票新闻并转 Sample 行；接口异常向上抛（调用方记失败清单）。"""
    import akshare as ak

    df = ak.stock_news_em(symbol=symbol)
    rows = []
    for _, rec in df.head(per_symbol).iterrows():
        url = str(rec.get("新闻链接", "") or "")
        if not url:
            continue
        rows.append(
            make_row(
                symbol=symbol,
                symbol_name=symbol_name,
                title=str(rec.get("新闻标题", "") or ""),
                content=str(rec.get("新闻内容", "") or ""),
                url=url,
                published_at=str(rec.get("发布时间", "") or ""),
                fetch_run_id=fetch_run_id,
                crawled_at=crawled_at,
                source=str(rec.get("文章来源", "") or "akshare"),
            )
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", help="逗号分隔股票代码，缺省用内嵌 25 只股票池")
    parser.add_argument("--per-symbol", type=int, default=PER_SYMBOL_DEFAULT)
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else list(DEFAULT_UNIVERSE)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    fetch_run_id = f"news_{now:%Y%m%d_%H%M%S}"
    crawled_at = now.isoformat(timespec="seconds")
    seen = load_seen_urls(out_path)

    all_new: list[dict] = []
    failures: list[str] = []
    for i, symbol in enumerate(symbols):
        symbol_name = DEFAULT_UNIVERSE.get(symbol, symbol)
        try:
            rows = fetch_symbol_rows(symbol, symbol_name, args.per_symbol, fetch_run_id, crawled_at)
        except Exception as exc:  # noqa: BLE001——单源失败入清单不阻塞整批
            failures.append(f"{symbol}: {type(exc).__name__}: {exc}")
            print(f"[{i + 1}/{len(symbols)}] {symbol} {symbol_name} 失败: {exc}")
        else:
            new = select_new(rows, seen)
            all_new.extend(new)
            print(f"[{i + 1}/{len(symbols)}] {symbol} {symbol_name} 取 {len(rows)} 新增 {len(new)}")
        if i < len(symbols) - 1:
            time.sleep(SLEEP_SECONDS)

    if all_new:
        with out_path.open("a", encoding="utf-8") as f:
            for row in all_new:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    total = len(load_seen_urls(out_path))
    print(f"\n完成：新增 {len(all_new)}，库内累计 {total}，失败 {len(failures)}")
    for line in failures:
        print(f"  失败: {line}")
    return 1 if len(failures) == len(symbols) else 0


if __name__ == "__main__":
    sys.exit(main())
