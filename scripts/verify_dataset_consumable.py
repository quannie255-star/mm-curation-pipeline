"""验收：产出的数据集**能被现成轮子直接消费**（不用本项目任何代码）。

这是"训练数据生产系统"的最终判据 —— 产物必须能在**项目之外**用起来。
借的轮子：`datasets`（HuggingFace）+ `pyarrow` + torch DataLoader。

⚠️ 判据纪律：**不许用 mm_curation 的代码读**。如果验收脚本自己 import 本项目
   才能读通，那它就不是"可直接消费"，只是"自洽"。
   所以本脚本只 import 第三方库与标准库。

用法：python -X utf8 scripts/verify_dataset_consumable.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HUB_OFFLINE", "1")

DS_NAME = sys.argv[1] if len(sys.argv) > 1 else "news_zh_v2"
DS = ROOT / "datasets" / DS_NAME


_ROW_UNIT = "sample"


def main() -> int:
    mf = DS / "manifest.json"
    if not mf.exists():
        print("✗ 尚未构建数据集，先跑 scripts/build_dataset.py")
        return 1
    man = json.loads(mf.read_text(encoding="utf-8"))
    print(f"=== {man['name']}（schema v{man['schema_version']}）===")
    print(f"  样本 {man['n_samples']} | token {man['n_tokens']} | "
          f"{man['compression_chars_per_token']} 字符/token")
    global _ROW_UNIT
    unit = man.get("row_unit", "sample")
    _ROW_UNIT = unit
    print(f"  切分 {man['splits']}（单位={unit}） | 行数 {man.get('n_rows', 0)} "
          f"| shard {man['n_shards']} | 截断 {man['n_truncated']}")

    ok = True

    # ── 1) pyarrow 直接读（最基础：任何 Arrow 工具都能用）──
    print("\n[1] pyarrow.parquet 直接读")
    import pyarrow.parquet as pq

    shards = sorted((DS / "shards").glob("*.parquet"))
    if not shards:
        print("  ✗ 没有 shard 文件")
        return 1
    total = 0
    for s in shards:
        t = pq.read_table(s)
        total += t.num_rows
        if s == shards[0]:
            print(f"  schema: {pf.schema_arrow.names if (pf := pq.ParquetFile(s)) else []}")
            # 压缩信息在 **ParquetFile.metadata.row_group(i).column(j)** 上，
            # 不在 Field 上（第一版 `t.schema.field(...).compression` → AttributeError），
            # 也不在 ChunkedArray 上（第二版 `.chunks` → AttributeError）。
            # 先查 API 再写，不要猜。
            md = pq.ParquetFile(s).metadata
            comps = {
                md.row_group(0).column(j).compression
                for j in range(md.row_group(0).num_columns)
            }
            print(f"  压缩: {sorted(comps)}")
            print(f"  行数/row_group: {md.row_group(0).num_rows}")
    print(f"  ✓ {len(shards)} 个 shard，共 {total} 行")
    # ⚠️ 基准随row_unit 变：packing 时磁盘行=block，不是样本
    want_rows = man.get("n_rows") or (
        man["n_blocks"] if unit == "block" else man["n_samples"])
    if total != want_rows:
        print(f"  ✗ **行数与 manifest 不符**：磁盘 {total} != "
              f"manifest {want_rows}（单位={unit}）")
        ok = False

    # ── 2) HuggingFace datasets 直接 load（业界标准入口）──
    # ⚠️ **不能用目录路径**：`Dataset.from_parquet(dir)` 在 datasets 5.x
    # 报 FileNotFoundError（它要的是文件列表或 HF 标准 layout 目录）。
    # ⚠️ 且**必须按 split 显式传 data_files**：把全部文件一股脑传进
    # `data_files={'train': [...]}` 会让 val/test 被合进 train
    # （实测 163 条全进 train）—— 那等于没有切分。
    print("\n[2] datasets.load_dataset（HF 标准入口，无需本项目代码）")
    try:
        import datasets as hfds

        by_split: dict[str, list[str]] = {"train": [], "val": [], "test": []}
        for s in shards:
            sp = s.name.split("-")[0]
            by_split.setdefault(sp, []).append(str(s))
        print(f"  按 split 传文件: "
              f"{ {k: len(v) for k, v in by_split.items() if v} }")

        hf = hfds.load_dataset("parquet", data_files=by_split)
        loaded = {k: v.num_rows for k, v in hf.items()}
        print(f"  ✓ 载入 {loaded} | 列 {hf['train'].column_names}")
        # 切分必须与 manifest 一致（否则等于没切分）
        want = {k: v for k, v in man["splits"].items() if v > 0}
        got = {k: v for k, v in loaded.items() if v > 0}
        assert got == want, f"**切分不一致**：磁盘 {got} != manifest {want}"
        print("  ✓ 切分与 manifest 一致")

        ex = hf["train"][0]
        # 训练就绪的两种模式，列名不同：
        #   sample 级 → id / text / tokens（变长，需外部 padding）
        #   block 级  → block_id / input_ids（定长，可直接 batch）
        key = "input_ids" if unit == "block" else "tokens"
        # load_dataset 返回 DatasetDict → column_names 是 {split: [列]}，
        # 逐 split 查（第一版直接拿它当 list 查 → KeyError/永远不匹配）
        for sp in hf:
            assert key in hf[sp].column_names, (
                f"split {sp}：row_unit={unit} 时应有列 {key}，"
                f"实际 {hf[sp].column_names}")
        assert isinstance(ex[key], list), f"{key} 应为 list（可 collate）"
        if unit == "block":
            print(f"  block0: id={ex['block_id']} n_tokens={ex['n_tokens']} "
                  f"split={ex['split']} len={len(ex[key])}")
    except AssertionError as exc:
        print(f"  ✗ {exc}")
        ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ datasets 读取失败: {type(exc).__name__} {str(exc)[:200]}")
        ok = False

    # ── 3) torch DataLoader 能 collate（训练前的最后一关）──
    print("\n[3] torch DataLoader collate（训练前最后一关）")
    try:
        import torch
        from torch.utils.data import DataLoader
        from torch.utils.data import Dataset as TorchDataset

        class TorchView(TorchDataset):  # noqa: D401 — 最小只读适配
            """最小适配层：把 HF Dataset 包成 torch Dataset。"""

            def __init__(self, hfd):
                self.d = hfd

            def __len__(self):
                return self.d.num_rows

            def __getitem__(self, i):
                r = self.d[i]
                key = "input_ids" if _ROW_UNIT == "block" else "tokens"
                ids = torch.tensor(r[key], dtype=torch.long)
                if "loss_mask" in r:
                    # padding 位用 -100 → 交叉熵自动忽略（PyTorch 约定）
                    m = torch.tensor(r["loss_mask"], dtype=torch.long)
                    labels = torch.where(m.bool(), ids,
                                         torch.full_like(ids, -100))
                else:
                    labels = ids.clone()
                return {"input_ids": ids, "labels": labels}

        import datasets as hfds

        by_split = {"train": [str(s) for s in shards if s.name.startswith("train-")]}
        hf = hfds.load_dataset("parquet", data_files=by_split)["train"]
        # ⚠️ **必须遍历全部 batch**，不能只看第一个。
        # 实测踩过：第一个 batch 恰好全是满 block（512）→ 看着是绿的，
        # 但尾块 451 混进来就崩（`stack expects each tensor to be equal
        # size, but got [451] and [512]`）。**只测一个 batch 是假绿。**
        dl = DataLoader(TorchView(hf), batch_size=4, shuffle=True)
        seen = 0
        shapes = set()
        for batch in dl:
            shapes.add(tuple(batch["input_ids"].shape[1:]))
            seen += int(batch["input_ids"].shape[0])
            assert batch["input_ids"].shape == batch["labels"].shape, (
                "causal LM 要求 labels 与 input_ids 同形")
        print(f"  ✓ 遍历 {seen} 行 / {len(shapes)} 种末维长度 {sorted(shapes)}")
        assert seen == len(hf), f"遍历行数 {seen} != 数据集 {len(hf)}"
        assert len(shapes) == 1, (
            f"定长数据集不该出现多种末维长度：{sorted(shapes)}（变长则 batch 会崩）")
        if "loss_mask" in hf.column_names:
            #⚠️ 必须核**全量**，不能只核 train。
            # manifest 的 n_real_tokens_total 是三切分合计；第一版只查 train
            # →349671 vs 596397 直接报「口径没对齐」，
            # 看起来像数据坏了，其实是**判据只覆盖了 59%**。
            allhf = hfds.load_dataset(
                "parquet",
                data_files={sp: [str(s) for s in shards
                                if s.name.startswith(sp + "-")]
                            for sp in ("train", "val", "test")},
            )
            n_real = sum(int(allhf[sp][i]["loss_mask"].count(1))
                         for sp in allhf for i in range(len(allhf[sp])))
            n_all = sum(len(allhf[sp]) for sp in allhf)
            man_real = man.get("n_real_tokens_total")
            man_pad = man.get("n_padding_tokens_total")
            print(f"  ✓ 全量 loss_mask 有效 token {n_real} / {n_all} block"
                  f"（manifest 记 {man_real}）")
            assert n_all == man.get("n_rows"), (
                f"全量 block 数 {n_all} != manifest n_rows {man.get('n_rows')}")
            assert man_real == n_real, (
                f"loss_mask 有效位 {n_real} != manifest n_real_tokens_total "
                f"{man_real} —— padding 口径没对齐")
            if man_pad is not None:
                # real + pad 必须等于 定长 block 的名义长度（自洽性硬约束）
                nominal = n_all * man["pack_block_size"]
                assert man_real + man_pad == nominal, (
                    f"real {man_real} + pad {man_pad} = {man_real + man_pad} "
                    f"!= blocks×size {nominal} —— manifest 内部不自洽")
                print(f"  ✓ padding 口径自洽：real {man_real} + pad {man_pad} "
                      f"= {nominal}（padding {man['padding_pct']}%）")
        one = TorchView(hf)[0]
        print(f"  ✓ 单条 input_ids {tuple(one['input_ids'].shape)} "
              f"dtype={one['input_ids'].dtype}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ DataLoader 失败: {type(exc).__name__} {str(exc)[:200]}")
        ok = False

    # ── 4) 泄漏检查结论必须是 manifest 里记录的（且自洽）──
    print("\n[4] 泄漏检查自洽性")
    lk = man["leakage_check"]
    n_leak = len(lk.get("md5_leaks", [])) + len(lk.get("minhash_leaks", []))
    print(f"  manifest 记录泄漏 {n_leak} 条，clean={lk.get('clean')}")
    print(f"  分层键={man['split_method']}")
    if lk.get("clean") and n_leak:
        print("  ✗ clean=True 但有泄漏记录 —— 自相矛盾")
        ok = False

    # ── 5) 校验和：shard 未被篡改 ──
    print("\n[5] shard 校验和")
    import hashlib

    bad = []
    for name, want in man["shard_checksums"].items():
        p = DS / "shards" / name
        if not p.exists():
            bad.append(f"{name}: 缺失")
            continue
        got = hashlib.md5(p.read_bytes()).hexdigest()
        if got != want:
            bad.append(f"{name}: {got[:12]} != {want[:12]}")
    if bad:
        print(f"  ✗ {len(bad)} 个 shard 校验失败: {bad[:3]}")
        ok = False
    else:
        print(f"  ✓ {len(man['shard_checksums'])} 个 shard 校验和全部匹配")

    print()
    print("=" * 56)
    print("验收结果:", "✓ 通过 —— 数据集可在项目之外直接消费" if ok else "✗ 未通过")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
