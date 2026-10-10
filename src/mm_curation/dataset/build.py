"""训练/分析可直接消费的数据集制品生产（训练数据生产系统的产物层）。

定位
----
本项目此前产出的是**报告**（`cleaned.jsonl` + `funnel_stats.json` + `report.md`），
那回答的是「这条数据好不好、为什么丢」。**训练数据生产系统**要回答的是
「给我一个能直接喂给trainer / analyst 的东西」—— 产物形态完全不同。

本模块是那个转换层：`清洗产物JSONL → 标准数据集制品`。

借轮子，不造轮子
----------------
格式全部采用现成标准，不自创：
- **Parquet**（pyarrow）—— 列式、带 schema、带统计信息，Arrow 生态原生
- **zstd 压缩**—— 工业界默认，压缩率/速度平衡最好
- **manifest.json** —— 与 `benchmarks/*/manifest.json` 同一约定（已有 `leakage_check` 字段）
- **train/val/test 切分** —— 先切分后 tokenize，否则 test 会泄漏进 train

⚠️ 为什么不用 `datasets.Dataset.to_parquet` 直接落盘：它要求全量载入内存。
本项目语料规模会到百万级，必须**流式分片写出**（逐块积累到 shard 大小就落盘），
所以这里直接用 pyarrow 的 `ParquetWriter`。

硬门槛（构建时校验，不通过就raise，不静默降级）
------------------------------------------------
1. **round-trip 保真**：`decode(encode(text)) == text` 必须逐字节成立。
   实测`uer/gpt2-chinese-cluecorpussmall` 在这一步**0% 通过**
   （WordPiece decode 每字间插空格 → 语料被静默改写），
   故选型判据不是「压缩率」而是「能否还原」。
2. **切分前查泄漏**：train/val/test 之间的 md5 + MinHash 双检，
   复用的是 `benchmarks/*/manifest.json` 里已有的 `leakage_check` 口径。
3. **manifest 里不写没测过的数字**：每个字段都要有对应的产出脚本。

产物布局
--------
    datasets/<name>/
      manifest.json          唯一真相源：样本数/token 数/谱系/校验和/许可/切分/泄漏/训练结果
      shards/train-00000.parquet
      shards/val-00000.parquet
      shards/test-00000.parquet
      REPORT.md              清洗报告（附带，非主产物）

Schema（v1）
------------
  id            string   样本唯一 id
  text          string   清洗后正文（round-trip 已验证）
  n_tokens      int32    该样本 token 数
  tokens        binary   token id 序列（variable_length 二进制，或直接存 uint32 list）
  split         string   train/val/test
  source        string   来源（文件名/symbol/时间）
  meta          struct   透传原始 meta（symbol/url/title 等）

用法
----
    python -X utf8 scripts/build_dataset.py \
        --input data/processed/finance_news_funnel/cleaned.jsonl \
        --name finance_news_v1 \
        --tokenizer Qwen/Qwen2.5-0.5B-Instruct \
        --val-ratio 0.1 --test-ratio 0.1
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 单个 shard 的目标行数。太小→文件多、元数据开销大；太大→无法流式。
DEFAULT_SHARD_ROWS = 2000


# 与仓库既有约定一致：JSONL 一律 split("\n")，禁用 splitlines()（U+2028 陷阱）
def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            yield json.loads(line)


@dataclass
class DatasetManifest:
    """数据集清单—— **唯一真相源**。

    ⚠️ 字段纪律：这里每个数字都必须由`build_dataset()` 实测写入，
    不允许调用方手填（手填的manifest 就是「没门禁的数字来源」）。
    """

    name: str
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""

    # ── 规模 ──
    n_samples: int = 0
    n_tokens: int = 0
    n_chars: int = 0
    compression_chars_per_token: float = 0.0

    # ── 谱系（这个数据集怎么来的）──
    source_files: list[str] = field(default_factory=list)
    funnel_config: str = ""
    funnel_ops: list[str] = field(default_factory=list)
    tokenizer: str = ""
    # embedding 词表大小 = **len(tokenizer)**（含 added tokens）。
    tokenizer_vocab_size: int = 0
    # 差值留档：len(tokenizer) - tokenizer.vocab_size。>0 说明该 tokenizer
    # 有 added special tokens，用 vocab_size 建 embedding 会越界。
    tokenizer_base_vocab_size: int = 0

    # ── 切分 ──
    # ⚠️ `splits` 的**单位由 `row_unit` 决定**，不是一个固定含义：
    #   row_unit="sample" → 样本数（不packing，一条样本一行）
    #   row_unit="block"  → block 数（packing，一条样本被拼进多个 block）
    # 踩过一次：packing 后 `splits` 还记样本数（1559/215/155），
    # 而磁盘上是block（694/281/207）→ 消费者按 manifest 核对必然对不上，
    # 只能靠猜「哪个才是真的」→ 数据集不可信。所以单位必须**显式登记**。
    splits: dict[str, int] = field(default_factory=dict)
    row_unit: str = "sample"  # "sample" | "block"
    n_rows: int = 0  # 磁盘 shard 里的实际行数（== sum(splits)）
    split_method: str = "hash-bucket(分层见split_key)"
    max_tokens_per_sample: int | None = None
    n_truncated: int = 0  # 被截断的样本数——**截断量必须被看见**，不能默默吃掉
    # ── packing（训练就绪）──
    pack_block_size: int | None = None  # None = 不 packing（保留样本级）
    n_blocks: dict[str, int] = field(default_factory=dict)
    packing_eos_token_id: int | None = None
    # padding 口径：定长 block 会引入 padding，必须让消费者能算出真实 token。
    # 不登记它 → 消费者拿 n_tokens 当语料 token 用，算出的训练量是虚高的
    # （512 vs 451 = 13% 虚高，且**每批都不一样**）。
    n_real_tokens_total: int = 0
    n_padding_tokens_total: int = 0
    padding_pct: float = 0.0

    # ── 泄漏检查（复用 benchmarks 的口径）──
    leakage_check: dict[str, Any] = field(default_factory=dict)

    # ── 完整性 ──
    shard_checksums: dict[str, str] = field(default_factory=dict)
    n_shards: int = 0

    # ── 训练结果（生产系统的核心：数据集必须对「值不值得训」负责）──
    # ⚠️ 未跑训练前必须是 null，**不许填占位数字**。
    training_runs: list[dict[str, Any]] = field(default_factory=list)

    license: str = "unknown"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def minhash_signature(text: str, num_perm: int = 64, prefix: int = 400) -> bytes:
    """轻量 MinHash 签名（字节 4-gram），用于近似泄漏检测。

    与 `dedup_fast` 同族算法但**独立实现**——这里要的是签名（做集合比较），
    不是完整去重管线（那个在 dedup_fast）。字长前缀截断是网页文本去重的标准做法。
    """
    prime = (1 << 61) - 1
    data = text.encode("utf-8")[:prefix].ljust(4, b"\x00")
    sig = []
    for i in range(num_perm):
        h = (i * 0x9E3779B97F4A7C15 + 0xBF58476D1CE4E5B9) % prime
        m = prime
        for j in range(len(data) - 3):
            k = data[j] | (data[j + 1] << 8) | (data[j + 2] << 16) | (data[j + 3] << 24)
            m = min(m, (h ^ k) % prime)
        sig.append(m)
    return b"".join(x.to_bytes(8, "little") for x in sig)


def estimate_jaccard(sig_a: bytes, sig_b: bytes) -> float:
    """由签名估计 Jaccard（相同位比例）。"""
    if len(sig_a) != len(sig_b):
        raise ValueError("签名长度不一致")
    n = len(sig_a) // 8
    same = sum(1 for i in range(n) if sig_a[i * 8 : i * 8 + 8] == sig_b[i * 8 : i * 8 + 8])
    return same / n


def pack_sequences(
    token_lists: list[list[int]], block_size: int, eos_token_id: int
) -> list[list[int]]:
    """把变长样本拼成定长 block（packing）——**训练数据系统的核心需求**。

    为什么必须有这一步（不是优化，是可用性）：
    变长 token 序列**无法直接 collate**。实测 `DataLoader(batch_size=4)` 报
    `stack expects each tensor to be equal size, but got [119] ... [112]` ——
    也就是说「能读出来」不等于「能训练」。工业界通解就是 packing：
    把多条样本首尾相接塞进固定长度 block，训练时按 block 走。

    纪律：
    - 每条样本后追加 EOS，模型才学得到「文档边界」（否则跨文档续写）
    - 不足一个 block 的尾部**丢弃**并记进manifest（不能静默截断）
    - **不跨 split 打包**：train 与val 的样本绝不能进同一个 block
    """
    out: list[list[int]] = []
    cur: list[int] = []
    for ids in token_lists:
        seq = list(ids) + [eos_token_id]
        if len(seq) > block_size:
            # 单条超长：切成若干完整 block。
            # 尾部不足 block_size 的部分**丢弃**（不能静默当成完整 block，
            # 也不能pad —— pad 会让模型学到大量 pad token）。
            n_full = len(seq) // block_size
            for i in range(n_full):
                out.append(seq[i * block_size : (i + 1) * block_size])
            cur = []
            continue
        if len(cur) + len(seq) > block_size:
            out.append(cur)
            cur = []
        cur.extend(seq)
    if cur:
        out.append(cur)  # 尾块：允许短，collate 时 pad 到 block_size
    return out


def assign_split(
    sample_id: str, val_ratio: float, test_ratio: float, key: str | None = None
) -> str:
    """确定性切分：同一个 id 永远落进同一个 split（可复现）。

    `key` 给定时**只按该字段分层**（如 finance 的 symbol）——
    **同一 symbol 的样本必须落在同一个 split**，否则同一支股票的新闻会
    同时出现在 train 与 test，泄漏的是「实体」而不是「样本」。

    ⚠️⚠️ 本函数第一版把 `sample_id` 也拼进了 hash basis
    （`f"{key}::{sample_id}"`），结果**分层完全失效**：
    实测 25 个 symbol 里有 **21 个跨 split**（含 1 个 symbol 同时出现在
    train/val/test 三处）。因为每条样本各自算自己的 hash，
    同一 symbol 的不同样本必然落到不同桶。
    拼接 id 等于「没有分层」——而且它**看起来是实现了分层的**，
    最坏的一种：静默失效、指标正常、报告照出。
    **正确做法：给了 key 就只 hash key。**
    """
    basis = key if key else sample_id
    h = int(hashlib.sha256(str(basis).encode("utf-8")).hexdigest()[:8], 16)
    frac = (h % 10_000) / 10_000
    if frac < val_ratio:
        return "val"
    if frac < val_ratio + test_ratio:
        return "test"
    return "train"


class DatasetBuilder:
    """流式构建数据集制品。

    流式的原因：语料会到百万级，全量载入内存不可接受。
    做法：逐条 tokenize → 累积到 `shard_rows` → 落一个 parquet shard。
    """

    def __init__(
        self,
        name: str,
        tokenizer,
        out_dir: str | Path,
        *,
        tokenizer_name: str = "",
        source_files: list[str] | None = None,
        funnel_config: str = "",
        funnel_ops: list[str] | None = None,
        license_: str = "unknown",
        notes: str = "",
        shard_rows: int = DEFAULT_SHARD_ROWS,
        val_ratio: float = 0.1,
        test_ratio: float = 0.1,
        split_key: str | None = None,
        max_tokens_per_sample: int | None = 4096,
        pack_block_size: int | None = None,
        roundtrip_sample: int = 50,
        strict_roundtrip: bool = True,
        compression: str = "zstd",
    ):
        self.name = name
        self.tok = tokenizer
        self.out_dir = Path(out_dir)
        self.tokenizer_name = tokenizer_name or getattr(tokenizer, "name_or_path", "?")
        # ⚠️ **必须用 len(tokenizer)，不是 tokenizer.vocab_size**。
        # 实测踩过：Qwen2.5 的 vocab_size=151643，但 len(tokenizer)=151665
        # —— `vocab_size` **不含 added tokens**（<|im_start|> 等），
        # 而编码时它们**会被产出**。按 vocab_size 建 embedding → 数据里
        # 出现 id=151645> 151643 → CUDA device-side assert
        # （`srcIndex < srcSelectDimSize` failed，报在训练循环深处，
        #  离真正的原因隔了十万八千里）。
        #
        # 判据：词表大小要能**容纳数据里出现过的最大 id**，不是
        #      「tokenizer 自己说它有多少词」。这条应该由**数据**验证。
        self.tokenizer_vocab_size = int(len(tokenizer))
        self.tokenizer_base_vocab_size = int(
            getattr(tokenizer, "vocab_size", self.tokenizer_vocab_size)
        )
        self.source_files = source_files or []
        self.funnel_config = funnel_config
        self.funnel_ops = funnel_ops or []
        self.license = license_
        self.notes = notes
        self.shard_rows = shard_rows
        self.val_ratio = val_ratio
        self.test_ratio = test_ratio
        self.split_key = split_key
        self.max_tokens = max_tokens_per_sample
        self.pack_block_size = pack_block_size
        # packing 需要 EOS id 来标记文档边界；缺了就raise 而不是默默不pack
        self.eos_id: int | None = None
        if pack_block_size:
            eos = getattr(tokenizer, "eos_token_id", None)
            if eos is None:
                raise ValueError(
                    f"pack_block_size={pack_block_size} 要求 tokenizer 有 eos_token_id，"
                    f"但 {self.tokenizer_name} 的 eos_token_id=None。"
                    "没有文档边界模型会学会跨文档续写 —— **不要静默降级为不 packing**。"
                )
            self.eos_id = int(eos)
        self._block_counts: dict[str, int] = {"train": 0, "val": 0, "test": 0}
        self._n_real_tokens = 0
        self._n_pad_tokens = 0
        self.roundtrip_sample = roundtrip_sample
        self.strict_roundtrip = strict_roundtrip
        self.compression = compression

        self._writers: dict[str, Any] = {}
        self._buffers: dict[str, list[dict]] = {}
        self._shard_idx: dict[str, int] = {"train": 0, "val": 0, "test": 0}
        self._sigs: dict[str, list[tuple[str, bytes]]] = {"train": [], "val": [], "test": []}
        self._shard_checksums: dict[str, str] = {}
        self._rt_checked = 0
        self._rt_fail = 0
        self.n_truncated = 0

        # 统计（finalize 时快照进 manifest）
        self.n_samples = 0
        self.n_tokens = 0
        self.n_chars = 0
        self.splits: dict[str, int] = {"train": 0, "val": 0, "test": 0}
        self.manifest: DatasetManifest | None = None

        # ⚠️ 构建**开始前**就清旧 shard（不是结束时）——
        # 中途失败时目录里不会留半成品被误当成有效产物。
        # 反过来放finalize 的话：崩了就崩在旧数据+半截新数据混合的状态，
        # 而 manifest 可能还是上一次写的 → 校验和能过、数据是错的。
        sd = self.out_dir / "shards"
        sd.mkdir(parents=True, exist_ok=True)
        for old in list(sd.glob("*.parquet")) + list(sd.glob("*.parquet.tmp")):
            old.unlink()

    # ── round-trip 门禁 ──
    def _check_roundtrip(self, text: str) -> None:
        if self._rt_checked >= self.roundtrip_sample:
            return
        got = self.tok.decode(
            self.tok(text, add_special_tokens=False)["input_ids"], skip_special_tokens=True
        )
        self._rt_checked += 1
        if got != text:
            self._rt_fail += 1
            if self.strict_roundtrip:
                raise ValueError(
                    f"round-trip 失败：tokenizer 不能逐字节还原语料。\n"
                    f"  原: {text[:60]!r}\n  回: {got[:60]!r}\n"
                    f"  → 数据集生产**绝不能**用这种 tokenizer（语料被静默改写）。"
                )

    def add(self, record: dict[str, Any]) -> None:
        text = (record.get("text") or "").strip()
        if not text:
            return
        self._check_roundtrip(text)

        ids = self.tok(text, add_special_tokens=False)["input_ids"]
        if self.max_tokens and len(ids) > self.max_tokens:
            # 截断而非丢弃：一条超长文档（如 1024+ token 的新闻全文）不该整条消失。
            # 记录比例，manifest 里报出来 —— **截断量是要被看见的**。
            ids = ids[: self.max_tokens]
            self.n_truncated += 1

        sid = str(record.get("id") or text_sha(text))
        meta = record.get("meta") or {}
        key = str(meta.get(self.split_key)) if self.split_key else None
        split = assign_split(sid, self.val_ratio, self.test_ratio, key)

        row = {
            "id": sid,
            "text": text,
            "n_tokens": len(ids),
            "tokens": ids,
            "split": split,
            "source": str(record.get("source") or ""),
            # schema 里 meta 声明为 string → 必须序列化。
            # ⚠️ 这里踩过一次：声明 string 却直接塞 dict，pyarrow 在**写入时**才报
            # ArrowTypeError（不是构建开始时）→ 前面 2000 条已经算了半天才炸。
            # **列式写入的错误暴露在 flush 时刻，不在构造时刻** —— 小样本试跑是必须的。
            "meta": json.dumps(meta, ensure_ascii=False, sort_keys=True) if meta else "",
        }
        # ⚠️ **token id 必须落在 embedding 词表内**，越界即raise。
        # 越界的症状出现在**训练时的 CUDA device-side assert**
        # （`srcIndex < srcSelectDimSize`），离真正的原因隔了十万八千里：
        # 构建期明明有数据在手，却什么都没查。
        # 判据取**数据**（max id）而不是「tokenizer 说它多大」——
        # 前者是真实约束，后者只是声明。
        n_tok = len(ids)
        if n_tok:
            lo, hi = min(ids), max(ids)
            if lo < 0 or hi >= self.tokenizer_vocab_size:
                raise ValueError(
                    f"token id 越界：[{lo}, {hi}] 不在 [0, "
                    f"{self.tokenizer_vocab_size}) 内（样本 {sid}）。"
                    "**词表口径错了**——embedding 大小必须用 len(tokenizer)，"
                    "vocab_size 不含 added tokens"
                )
        self._buffers.setdefault(split, []).append(row)
        self._sigs[split].append((sid, minhash_signature(text)))
        self._bump(len(ids), len(text))
        if len(self._buffers[split]) >= self.shard_rows:
            self._flush(split)

    def _schema(self):
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("n_tokens", pa.int32()),
                pa.field("tokens", pa.list_(pa.int32())),
                pa.field("split", pa.string()),
                pa.field("source", pa.string()),
                pa.field("meta", pa.string()),  # JSON 字符串：schema 稳定优先于嵌套灵活性
            ]
        )

    def _flush(self, split: str) -> None:
        buf = self._buffers.get(split)
        if not buf:
            return
        import pyarrow as pa

        idx = self._shard_idx[split]
        name = f"{split}-{idx:05d}.parquet"
        path = self.out_dir / "shards" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(buf, schema=self._schema())
        pq = __import__("pyarrow.parquet", fromlist=["ParquetWriter"])
        tmp = path.with_suffix(".parquet.tmp")
        with pq.ParquetWriter(str(tmp), self._schema(), compression=self.compression) as w:
            w.write_table(table)
        tmp.replace(path)  # 原子落盘：避免半截文件被当成有效 shard
        self._shard_checksums[name] = md5_bytes(path.read_bytes())
        self._shard_idx[split] = idx + 1
        self._buffers[split] = []

    def check_leakage(self, jaccard_threshold: float = 0.8) -> dict[str, Any]:
        """切分间泄漏检查：md5 精确 + MinHash 近似（复用 benchmarks 的口径）。"""
        out: dict[str, Any] = {
            "jaccard_threshold": jaccard_threshold,
            "md5_leaks": [],
            "minhash_leaks": [],
            "by_pair": {},
        }
        # md5 精确
        seen: dict[str, str] = {}
        for split in ("train", "val", "test"):
            for sid, _ in self._sigs[split]:
                if sid in seen and seen[sid] != split:
                    out["md5_leaks"].append(sid)
                seen[sid] = split
        # MinHash 近似：val/test 逐条与 train 比（train 侧建索引）
        train_by_bucket: dict[int, list[tuple[str, bytes]]] = {}
        for sid, sig in self._sigs["train"]:
            train_by_bucket.setdefault(sig[0], []).append((sid, sig))
        for split in ("val", "test"):
            pair: dict[str, int] = {"candidates": 0, "leaks": 0}
            for sid, sig in self._sigs[split]:
                cand = train_by_bucket.get(sig[0])
                if not cand:
                    continue
                pair["candidates"] += 1
                for tid, tsig in cand:
                    if estimate_jaccard(sig, tsig) >= jaccard_threshold:
                        out["minhash_leaks"].append({"sample": sid, "train": tid})
                        pair["leaks"] += 1
                        break
            out["by_pair"][f"train-{split}"] = pair
        out["n_leaks_total"] = len(out["md5_leaks"]) + len(out["minhash_leaks"])
        out["clean"] = out["n_leaks_total"] == 0
        return out

    def _pack_schema(self):
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("block_id", pa.string()),
                pa.field("input_ids", pa.list_(pa.int32())),
                pa.field("loss_mask", pa.list_(pa.int8())),
                pa.field("n_tokens", pa.int32()),
                pa.field("n_real_tokens", pa.int32()),
                pa.field("split", pa.string()),
            ]
        )

    def _write_packed_shards(self) -> None:
        """产出 block 级 shard —— 这才是**能直接训练**的数据集。

        与样本级 shard 的关系：manifest 里 `n_samples`/`n_tokens` 是**语料口径**，
        `n_blocks` 是**训练口径**。两者必须分开报：混在一起就会出现
        「样本数 < block 数」这种看不懂的数字。
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        for split in ("train", "val", "test"):
            rows = self._buffers.get(split) or []
            if not rows:
                continue
            # **绝不跨 split 打包**
            blocks = pack_sequences([r["tokens"] for r in rows], self.pack_block_size, self.eos_id)
            # ⚠️ **必须 pad 到定长**，否则 batch 一定崩。
            # 实测踩过：尾块只有 451 token，`DataLoader(shuffle=True)` 立刻报
            # 「stack expects each tensor to be equal size, but got [451] and [512]」。
            # 而顺序读时前几个 block 恰好都是满的 → 看着是绿的（**假绿**）。
            # 定长 + loss_mask 是业界标准（HF/gpt-neox 都这么干）：
            # padding 位 input_ids 填 eos，loss_mask=0 → 不参与 loss。
            # 代价：manifest 的 n_tokens 是语料口径，不含 padding，
            # 必须另给 n_real_tokens 让消费者知道真实 token 量。
            bs = self.pack_block_size
            pad_id = self.eos_id
            out_rows = []
            for i, b in enumerate(blocks):
                n_real = len(b)
                if n_real < bs:
                    ids = list(b) + [pad_id] * (bs - n_real)
                    mask = [1] * n_real + [0] * (bs - n_real)
                else:
                    ids, mask = list(b), [1] * n_real
                self._n_real_tokens += n_real
                self._n_pad_tokens += bs - n_real
                out_rows.append(
                    {
                        "block_id": f"{split}-{i:06d}",
                        "input_ids": ids,
                        "loss_mask": mask,
                        "n_tokens": bs,
                        "n_real_tokens": n_real,
                        "split": split,
                    }
                )
            self._block_counts[split] = len(out_rows)
            # 分批写，避免一次过大
            batch = 1000
            for start in range(0, len(out_rows), batch):
                idx = start // batch
                name = f"{split}-{idx:05d}.parquet"
                path = self.out_dir / "shards" / name
                table = pa.Table.from_pylist(
                    out_rows[start : start + batch], schema=self._pack_schema()
                )
                tmp = path.with_suffix(".parquet.tmp")
                with pq.ParquetWriter(
                    str(tmp), self._pack_schema(), compression=self.compression
                ) as w:
                    w.write_table(table)
                tmp.replace(path)
                self._shard_checksums[name] = md5_bytes(path.read_bytes())
            self._buffers[split] = []

    def finalize(self) -> None:
        """落盘 manifest。所有数字由本次构建实测写入，无占位。"""
        # 旧 shard 已在 __init__ 清理（构建开始前，避免中途失败留半成品）
        if self.pack_block_size:
            self._write_packed_shards()
        else:
            for split in ("train", "val", "test"):
                self._flush(split)

        leakage = self.check_leakage()
        # packing 时磁盘行 = block，manifest 必须跟着换单位（见字段注释）
        packed = bool(self.pack_block_size)
        split_counts = dict(self._block_counts) if packed else self._samples_per_split()
        real_total = self._n_real_tokens if packed else self.n_tokens
        pad_total = self._n_pad_tokens if packed else 0
        man = DatasetManifest(
            name=self.name,
            created_at=_now_iso(),
            n_samples=self.n_samples,
            n_tokens=self.n_tokens,
            n_chars=self.n_chars,
            compression_chars_per_token=(
                round(self.n_chars / self.n_tokens, 3) if self.n_tokens else 0.0
            ),
            source_files=self.source_files,
            funnel_config=self.funnel_config,
            funnel_ops=self.funnel_ops,
            tokenizer=self.tokenizer_name,
            tokenizer_vocab_size=self.tokenizer_vocab_size,
            tokenizer_base_vocab_size=self.tokenizer_base_vocab_size,
            splits=split_counts,
            row_unit="block" if packed else "sample",
            n_rows=sum(split_counts.values()),
            split_method=(
                f"sha256 bucket（分层键={self.split_key}）"
                if self.split_key
                else "sha256 bucket（无分层）"
            ),
            max_tokens_per_sample=self.max_tokens,
            n_truncated=self.n_truncated,
            pack_block_size=self.pack_block_size,
            n_blocks=dict(self._block_counts),
            packing_eos_token_id=self.eos_id,
            n_real_tokens_total=real_total,
            n_padding_tokens_total=pad_total,
            padding_pct=(
                round(pad_total / (real_total + pad_total) * 100, 3)
                if (real_total + pad_total)
                else 0.0
            ),
            leakage_check=leakage,
            shard_checksums=self._shard_checksums,
            n_shards=len(self._shard_checksums),
            license=self.license,
            notes=self.notes,
        )
        # 训练结果字段保持空list —— **不许填占位**。跑完训练由
        # `scripts/eval_training_utility.py --dataset-manifest` 回填。
        (self.out_dir / "manifest.json").write_text(
            json.dumps(man.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.manifest = man

    # ── 统计（构建期累加，finalize 时快照）──
    def _bump(self, n_tokens: int, n_chars: int) -> None:
        self.n_samples += 1
        self.n_tokens += n_tokens
        self.n_chars += n_chars
        sp = self._samples_per_split()
        self.splits = dict(sp)

    def _samples_per_split(self) -> dict[str, int]:
        return {s: len(self._sigs[s]) for s in ("train", "val", "test")}
