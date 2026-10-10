"""上传检查器的元测试 —— 检查器不能「什么都不拒」。

每个检查都有两侧：
- **拒**：喂一份真的坏的，必须 `ok=False` 且给出**可操作**的原因；
- **放**：喂一份真的好的，必须 `ok=True`。
只有两侧都在，才证明拒的不是「格式的普遍情况」而是真的问题。

⚠️ 这条测试历史上最容易犯的错：只测「坏文件被拒」，
结果实现写`return True` 也全绿 —— 恒真。
所以这里每一对都成对写，且放行侧用**不同的坏点**做区分
（例如空值vs 缺键vs 键名错），避免两个坏点撞成同一个原因。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mm_curation.studio.checkers import check_upload  # noqa: E402
from mm_curation.studio.recipes import BY_KEY, RECIPES, assert_recipes_valid  # noqa: E402

TEXT = ("text_article", ("text",))


def _write(tmp_path: Path, rows: list, name: str = "d.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                 encoding="utf-8")
    return p


# ── 配方层：算子名必须真实注册（import 时已跑，这里再钉一次）──


def test_配方自检通过():
    assert_recipes_valid()
    assert len(RECIPES) >= 4


def test_每个配方声明必填字段():
    """没有必填字段的配方 = 无法校验上传 = 前端会放行一切。"""
    for r in RECIPES:
        assert r.required_fields, f"{r.key} 没有声明必填字段"
        assert r.blurb and r.produces and r.sample_hint, f"{r.key} 缺说明文字"


def test_配方算子都是注册表里真实存在的():
    from curation_eval.registry import available_operator_metas

    import mm_curation.operators  # noqa: F401

    known = set(available_operator_metas())
    for r in RECIPES:
        for op, _ in r.operators:
            assert op in known, f"{r.key} 用了未注册算子 {op}"


def test_配方算子不重复():
    """漏斗里重复算子 = 白跑一遍还多花一份钱。"""
    for r in RECIPES:
        ops = [op for op, _ in r.operators]
        assert len(ops) == len(set(ops)), f"{r.key} 有重复算子"


# ── 拒：真的坏的 ──────────────────────────────────────────────────


def test_拒空文件(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text("", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is False and "空" in r.fatal


def test_拒整行不是JSON(tmp_path):
    p = tmp_path / "b.jsonl"
    p.write_text("这不是 json\n也不是\n", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is False
    assert "JSON" in r.fatal


def test_拒缺必填字段(tmp_path):
    """JSONL 键名拼错是最常见失败，必须给出「哪个字段缺」而不是一句「格式错」。"""
    p = _write(tmp_path, [{"txt": "内容"}, {"txt": "另一条"}])
    r = check_upload(p, ("text",))
    assert r.ok is False
    assert "`text`" in r.fatal
    assert r.missing_fields.get("text") == 2


def test_拒必填字段全空(tmp_path):
    """键在但全空 —— 与「缺键」是**不同的病**，提示也要不同。"""
    p = _write(tmp_path, [{"text": ""}, {"text": "   "}])
    r = check_upload(p, ("text",))
    assert r.ok is False
    assert r.empty_fields.get("text") == 2
    assert "空值" in r.fatal


def test_拒JSONL其实传了CSV(tmp_path):
    """后缀与内容不符，要明确建议改名，否则用户会一直试。"""
    p = tmp_path / "d.jsonl"
    p.write_text("text\n第一条\n第二条\n", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is False
    assert "CSV" in r.fatal or "JSON" in r.fatal


def test_拒CSV缺必填列(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("txt,label\na,b\n", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is False and "`text`" in r.fatal


def test_拒图文场景图片全找不到(tmp_path):
    """图文场景：字段齐但图全丢 → 必须拒，否则清洗时静默丢掉全部。"""
    p = _write(tmp_path, [{"image_path": "images/a.jpg", "text": "x"},
                          {"image_path": "images/b.jpg", "text": "y"}])
    r = check_upload(p, ("image_path", "text"), image_root=tmp_path / "no_such_dir")
    assert r.ok is False
    assert "图片" in r.fatal


def test_拒坏行混在里面但不拒整份(tmp_path):
    """部分行坏掉时**不该拒整份** —— 应放行并提示跳过了几行。"""
    p = tmp_path / "mixed.jsonl"
    good = json.dumps({"text": "第一条"}, ensure_ascii=False)
    p.write_text(good + "\n{坏行\n" + good + "\n", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is True
    assert r.n_bad_json == 1
    assert any("跳过" in n for n in r.notes)


# ── 放：真的好的（证明上面的拒不是恒真）──────────────────────────


def test_放行合法JSONL(tmp_path):
    p = _write(tmp_path, [{"text": "第一条"}, {"text": "第二条"}, {"text": "第三条"}])
    r = check_upload(p, ("text",))
    assert r.ok is True, r.fatal
    assert r.n_rows == 3


def test_放行合法CSV(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("text,label\n第一条,a\n第二条,b\n", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is True, r.fatal
    assert r.n_rows == 2


def test_放行带BOM的CSV(tmp_path):
    """Excel 导出的中文 CSV 一定带 BOM —— 不剥会让第一个字段名变成 '﻿text'。"""
    p = tmp_path / "bom.csv"
    p.write_bytes("text,label\n第一条,a\n".encode("utf-8-sig"))
    r = check_upload(p, ("text",))
    assert r.ok is True, r.fatal


def test_放行分号分隔的CSV(tmp_path):
    """从数据库导出的CSV 常用分号。"""
    p = tmp_path / "semi.csv"
    p.write_text("text;label\n第一条;a\n第二条;b\n", encoding="utf-8")
    r = check_upload(p, ("text",))
    assert r.ok is True, r.fatal


def test_放行带额外字段并提示(tmp_path):
    """字段全在声明范围内 → 放行；出现**未声明**的字段 → 放行但提示（不拦）。"""
    p = _write(tmp_path, [{"text": "x", "id": "1", "labels": {"dirty": ""}}])
    r = check_upload(p, ("text",), ("id", "labels"))
    assert r.ok is True, r.fatal
    # text/id/labels 都已声明 → 不该有多余字段提示
    assert not any("用不上" in n for n in r.notes), r.notes

    p2 = _write(tmp_path, [{"text": "x", "source_url": "https://a"}])
    r2 = check_upload(p2, ("text",), ("id",))
    assert r2.ok is True, r2.fatal
    assert any("用不上" in n for n in r2.notes), r2.notes
    assert any("source_url" in n for n in r2.notes)


def test_图文场景图片存在时放行(tmp_path):
    imgs = tmp_path / "images"
    imgs.mkdir()
    (imgs / "a.jpg").write_bytes(b"fake")
    p = _write(tmp_path, [{"image_path": "images/a.jpg", "text": "x"}])
    r = check_upload(p, ("image_path", "text"), image_root=tmp_path)
    assert r.ok is True, r.fatal


def test_单个坏图不拒整份(tmp_path):
    """一张图缺失只影响一条样本，不该把整个文件拒掉。"""
    imgs = tmp_path / "images"
    imgs.mkdir()
    (imgs / "a.jpg").write_bytes(b"fake")
    p = _write(tmp_path, [{"image_path": "images/a.jpg", "text": "x"},
                          {"image_path": "images/gone.jpg", "text": "y"}])
    r = check_upload(p, ("image_path", "text"), image_root=tmp_path)
    assert r.ok is True, r.fatal
    assert any("找不到" in n for n in r.notes)


# ── 每个配方都能用自己的必填字段做检查（不许配错）────────────────


@pytest.mark.parametrize("key", sorted(BY_KEY))
def test_每个配方的必填字段都能通过自己的检查(key):
    r = BY_KEY[key]
    sample = {f: "x" for f in r.required_fields}
    assert sample, f"{key} 没有必填字段"
    # 字段名不能是保留字冲突
    for f in r.required_fields:
        assert f.isascii(), f"{key} 的字段名 {f} 含非 ASCII（读文件时可能对不上）"
