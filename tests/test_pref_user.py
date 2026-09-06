"""θ 偏好判官工坊单测：v2 标注 / oracle 通道 / 最小对 / 按对 holdout / 对照题 / manifest。"""

from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

import pytest

from mm_curation.tuning.preference import (
    DEFAULT_PROTOCOL,
    MIN_PAIRS,
    SEED_USER,
    USER_PERSONA,
    build_user_pref_data,
    make_label_row,
    make_variant_pairs,
    oracle_labels_from_corpus,
    write_user_benchmark,
)

# 大词表随机组句（ε2 同款配方）：规避模板自相似——句式骨架复现的合成语料
# 会被 MinHash 泄漏检查判为真阳性近重复（实测 33/37 假泄漏，笔记 #63 同族）。
# 词表取 50×50=2500 个二字词：词内 4-gram 池足够大，随机句间 gram 期望重叠 ≈1%
_LEX = (
    "晨暮潮蝉叶雪汛收航练火流马货递图划签工运查护修账盘点广标算轨水林"
    "市管网道链步渡巷脊岸垄房栈车头隧篦牌线栏台架椅"
)
assert len(_LEX) == len(set(_LEX))
_WORDS = [a + b for a in _LEX for b in _LEX]


def _doc(i: int) -> dict:
    rng = random.Random(1000 + i)

    def sent(n: int) -> str:
        return "".join(rng.choice(_WORDS) for _ in range(n))

    body = (
        f"{sent(6)}，{sent(8)}。\n"
        f"据介绍，{sent(10)}，负责人{i % 9}某表示「{sent(4)}」。\n"
        f"背景是{sent(9)}，细节从略。"
    )
    return {"id": f"news{i:06d}", "title": f"标题{i}", "text": f"标题{i}：{sent(8)}\n\n{body}"}


def _labels(n: int, *, prefer: str = "S", protocol: str = DEFAULT_PROTOCOL) -> list[dict]:
    return oracle_labels_from_corpus(
        [_doc(i) for i in range(n)], protocol=protocol, prefer=prefer, n_docs=n
    )


def test_make_variant_pairs_and_oracle_rule():
    corpus = [_doc(i) for i in range(30)]
    pairs = make_variant_pairs(corpus)
    assert len(pairs) == 30
    rows = oracle_labels_from_corpus(corpus, prefer="S", n_docs=30)
    assert len(rows) == 30
    for r in rows:
        # v2 行完整性：全文落盘 + 变体元数据 + oracle 分账字段
        assert r["cand_a"] and r["cand_b"] and r["protocol"] == DEFAULT_PROTOCOL
        assert r["labeler"] == "oracle"
        assert {r["variant_a"], r["variant_b"]} == {"S", "F"}
        # 精炼派 oracle：点击的候选必须是 S 变体
        assert r["variant_a"] == "S" if r["choice"] == "甲" else r["variant_b"] == "S"
    # 50/50 位置随机化：两种位置都要出现
    assert {r["choice"] for r in rows} == {"甲", "乙"}
    with pytest.raises(ValueError, match="不足"):
        oracle_labels_from_corpus(corpus, n_docs=31)


def test_build_minimal_pair_and_prompt():
    triples, items, stats = build_user_pref_data(_labels(90))
    assert stats["n_valid"] == 90 and stats["n_reject"] == 0
    assert stats["n_eval_main"] == round(90 * 0.25)
    for t in (t for t in triples if t["kind"] == "main"):
        assert t["persona"] == USER_PERSONA
        obj_c, obj_r = json.loads(t["chosen"]), json.loads(t["rejected"])
        # 最小对：chosen/rejected 只差 甲/乙 字母（#60 纪律）
        assert {obj_c["choice"], obj_r["choice"]} == {"甲", "乙"}
        assert obj_c["reason"] == obj_r["reason"]
        assert "【候选甲】" in t["prompt"] and "【候选乙】" in t["prompt"]
    # 金标一致性：eval 题 gold 即该对点击
    for it in (it for it in items if it["kind"] == "main"):
        assert it["gold"] in ("甲", "乙")
        assert it["variant_map"][it["gold"]] == it["gold_variant"]


def test_holdout_by_pair_disjoint_and_reject_excluded():
    labels = _labels(100)
    for r in labels[:5]:
        r["choice"] = "REJECT"
    triples, items, stats = build_user_pref_data(labels)
    assert stats["n_reject"] == 5 and stats["n_labels"] == 100
    assert all(t["kind"] != "reject" for t in triples)
    # holdout 按对为单位：训练 prompt 与评测 prompt 零相交，source_id 零相交
    train_prompts = {t["prompt"] for t in triples}
    eval_prompts = {it["prompt"] for it in items}
    assert not (train_prompts & eval_prompts)
    train_src = {t["source_id"] for t in triples}
    eval_src = {it["source_id"] for it in items}
    assert not (train_src & eval_src)


def test_control_items_gold_is_clean():
    from mm_curation.tuning.preference import _BOILERPLATE

    triples, items, _ = build_user_pref_data(_labels(90))
    for t in (t for t in triples if t["kind"] == "control"):
        obj_c = json.loads(t["chosen"])
        # 金标（chosen）一侧不含任何损伤样板；另一侧含其中之一
        gold_side = "甲" if obj_c["choice"] == "甲" else "乙"
        other_side = "乙" if gold_side == "甲" else "甲"
        clean_text = t["prompt"].split(f"【候选{gold_side}】")[1].split("【候选")[0]
        damaged_text = t["prompt"].split(f"【候选{other_side}】")[1].split("【候选")[0]
        assert not any(b in clean_text for b in _BOILERPLATE)
        assert any(b in damaged_text for b in _BOILERPLATE)
        assert t["gold_variant"] == "clean"
    for it in (it for it in items if it["kind"] == "control"):
        assert it["gold_variant"] == "clean"
        assert set(it["variant_map"].values()) == {"clean", "damaged"}
        assert it["variant_map"][it["gold"]] == "clean"


def test_insufficient_and_protocol_mismatch():
    with pytest.raises(ValueError, match="不足"):
        build_user_pref_data(_labels(MIN_PAIRS - 1))
    labels = _labels(90)
    labels.append({**labels[0], "protocol": "另一种协议", "source_id": "x1"})
    with pytest.raises(ValueError, match="协议"):
        build_user_pref_data(labels)


def test_limit_learning_curve_channel():
    _, _, full = build_user_pref_data(_labels(100))
    # 学习曲线 50 点：limit 配 min_pairs 放宽下限（RUNBOOK 实验通道）
    _, _, limited = build_user_pref_data(_labels(100), limit=50, min_pairs=30)
    assert limited["n_valid"] == 50 < full["n_valid"]
    assert limited["n_eval_main"] == round(50 * 0.25)


def test_write_user_benchmark_manifest(tmp_path: Path):
    triples, items, stats = build_user_pref_data(_labels(90))
    dpo = tmp_path / "pref_user_dpo.jsonl"
    dpo.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in triples) + "\n", encoding="utf-8"
    )
    manifest = write_user_benchmark(items, tmp_path / "bench", train_jsonl=dpo, stats=stats)
    assert manifest["benchmark"] == "pref_user_v1"
    assert manifest["personas"] == {USER_PERSONA: DEFAULT_PROTOCOL}
    assert manifest["labeler"] == {"oracle": 90}
    # md5 泄漏必须为 0（精确指纹打在投影后的候选正文上）；minhash 在 CJK 上有
    # 字节级 4-gram 塌缩噪声（UTF-8 首字节仅 ~6 取值），只记录计数不作断言——
    # 结构性隔离由 test_holdout_by_pair_disjoint 的按对切分硬保证
    assert manifest["leakage_check"]["md5_leaks"] == []
    assert isinstance(manifest["leakage_check"]["minhash_leaks"], list)
    assert "真人" in manifest["label_protocol"]
    assert (tmp_path / "bench" / "items.jsonl").exists()
    # items 可被 eval 脚本格式消费：id/persona/kind/prompt/gold/variant_map/source_id
    for it in items:
        assert {"id", "persona", "kind", "prompt", "gold", "gold_variant", "source_id"} <= set(it)


def test_make_label_row_choice_guard():
    with pytest.raises(ValueError, match="choice"):
        make_label_row(
            protocol="p",
            source_id="s",
            cand_a="a",
            cand_b="b",
            variant_a="S",
            variant_b="F",
            choice="丙",
            labeler="human",
        )


def test_report_path_per_benchmark():
    spec = importlib.util.spec_from_file_location(
        "run_pref_benchmark",
        Path(__file__).resolve().parents[1] / "scripts" / "run_pref_benchmark.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.report_path("benchmarks/pref_user_v1").name == "pref_alignment_pref_user_v1.json"
    assert mod.report_path("benchmarks/pref_news_v1").name == "pref_alignment_pref_news_v1.json"


def test_user_seed_family_isolated():
    # seed 新族：不与训练 23 / judge 9000 / pref 31 / ext 41 / η-b' 47 撞车
    assert SEED_USER not in (23, 9000, 31, 41, 47)
