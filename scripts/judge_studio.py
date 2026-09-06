"""θ 偏好判官工坊：五步向导（导入 → 标注 → 训练 → 评测 → 试用）。

启动：streamlit run scripts/judge_studio.py
非开发者入口：全程无 YAML/命令行暴露；训练与评测 subprocess 复用现有脚本
（全默认参数）。数据纪律见 docs/design_tables.md θ 节：标注 v2 全文落盘；
真人标注（本向导）与模拟用户（build_oracle_labels.py）分文件分账。
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import torch  # noqa: E402, I001

from mm_curation.tuning.preference import (  # noqa: E402
    CANDIDATE_MAX_CHARS,
    DEFAULT_PROTOCOL,
    MIN_PAIRS,
    PREF_PROMPT,
    load_news_corpus_excluded,
    make_label_row,
    make_variant_pairs,
)
from run_pref_benchmark import parse_choice  # noqa: E402  （parse_choice 单一定义源）

LABELS = REPO / "data/annot/pref_labels_v2.jsonl"
DPO = "data/interim/pref_user_dpo.jsonl"
BENCH = "benchmarks/pref_user_v1"
REPORT = REPO / "data/reports/pref_alignment_pref_user_v1.json"
ADAPTER = REPO / "models/judge_pref_USER"
TRAIN_LOG = REPO / "runs/studio_train.log"
EVAL_LOG = REPO / "runs/studio_eval.log"
RECOMMEND, MAX_PAIRS = 500, 600  # 学习曲线实测：188 对未学会（≈通用）、488 对达标 0.839
BASE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
PLACEMENT_SEED = 20260906  # 甲/乙摆位固定 seed：同一批语料重复生成得到同一队列

S = st.session_state
S.setdefault("pairs", [])
S.setdefault("cursor", 0)
S.setdefault("protocol", DEFAULT_PROTOCOL)


def _read_labels() -> list[dict]:
    if not LABELS.exists():
        return []
    return [json.loads(ln) for ln in LABELS.read_text(encoding="utf-8").split("\n") if ln.strip()]


def _run_with_log(cmd: list[str], log_path: Path, total_hint: str) -> int:
    """subprocess 跑现有脚本，日志实时 tail（训练期间勿刷新——Streamlit 单线程阻塞）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    box, bar = st.empty(), st.progress(0.02, text=total_hint)
    shown = 0.02
    with log_path.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-X", "utf8", *cmd], cwd=REPO, stdout=lf, stderr=subprocess.STDOUT,
            env=env,
        )
        while proc.poll() is None:
            time.sleep(3)
            tail = log_path.read_text(encoding="utf-8", errors="ignore").split("\n")[-6:]
            box.code("\n".join(tail), language="text")
            m = re.findall(r"(\d+)%\|", tail[-1]) if tail else []
            shown = max(0.02, int(m[-1]) / 100) if m else min(0.99, shown + 0.01)
            bar.progress(shown, text=total_hint)
    return proc.returncode


def _docs_from_inputs(uploads, pasted: str) -> list[dict]:
    docs = []
    for f in uploads or []:
        text = f.read().decode("utf-8", errors="ignore").strip()
        if text:
            docs.append({"id": f"up-{f.name}", "title": text.split("\n", 1)[0][:40], "text": text})
    for n, chunk in enumerate(pasted.split("\n\n") if pasted.strip() else []):
        text = chunk.strip()
        if len(text) >= 100:
            docs.append({"id": f"paste-{n}", "title": text.split("\n", 1)[0][:40], "text": text})
    return docs


def _gen_pairs(docs: list[dict]) -> tuple[list[dict], int]:
    """文档 → 甲/乙摆位随机的候选对队列（S=标题+导语，F=全文）。返回 (队列, 可用率)。"""
    base = make_variant_pairs(docs)
    if not base:
        return [], 0
    rng = random.Random(PLACEMENT_SEED)
    rng.shuffle(base)
    pairs = []
    for p in base:
        if rng.randrange(2) == 0:
            pairs.append({**p, "cand_a": p["s"], "cand_b": p["f"],
                          "variant_a": "S", "variant_b": "F"})
        else:
            pairs.append({**p, "cand_a": p["f"], "cand_b": p["s"],
                          "variant_a": "F", "variant_b": "S"})
    return pairs[:MAX_PAIRS], round(100 * len(base) / max(len(docs), 1))


# ---- ① 导入 ----


def step_import():
    st.header("① 导入文本")
    st.caption("这一步做什么：把文章交给系统，系统把每篇自动改成「精简版」和「完整版」两个候选，稍后请你投票。")
    S.protocol = st.text_area(
        "你的偏好（用一句自己的话写，训练时会告知判官）", S.protocol, height=90
    ).strip()
    uploads = st.file_uploader("上传 txt / md 文件（可多选）", accept_multiple_files=True,
                               type=["txt", "md"])
    pasted = st.text_area("…或直接粘贴文章（空行分隔多篇）", height=130)
    c1, c2 = st.columns(2)
    if c1.button("用上面的文本生成候选对", type="primary", use_container_width=True):
        docs = _docs_from_inputs(uploads, pasted)
        S.pairs, rate = _gen_pairs(docs)
        if S.pairs:
            st.success(f"导入 {len(docs)} 篇 → 可用 {rate}% → "
                       f"生成 {len(S.pairs)} 个候选对，请进第②步。")
        else:
            st.error("没有可切分的文章：需要「首行标题 + 至少两段正文」且正文含数字或引语。")
    if c2.button("没有文本？一键用示例语料", use_container_width=True):
        corpus = load_news_corpus_excluded()
        if not corpus:
            st.error("本地没有示例语料（data/raw/news_corpus.jsonl）。先粘贴自己的文本，"
                     "或按 RUNBOOK 运行一次爬取脚本。")
        else:
            S.pairs, rate = _gen_pairs(corpus)
            st.success(f"示例语料 {len(corpus)} 篇（已排除其他任务占用）→ 可用 {rate}% → "
                       f"生成 {len(S.pairs)} 个候选对，请进第②步。")
    if S.pairs:
        st.info(f"当前队列：{len(S.pairs)} 对。每个候选训练时取前 {CANDIDATE_MAX_CHARS} 字。")


# ---- ② 标注 ----


def step_annotate():
    st.header("② 点击标注")
    st.caption(f"这一步做什么：读两个候选，点出你更喜欢的一个；两个都不合格就点「都不合格」。"
               f"建议至少 {RECOMMEND} 次，最低 {MIN_PAIRS} 次。")
    labels = _read_labels()
    if labels:
        n_reject = sum(1 for r in labels if r["choice"] == "REJECT")
        st.caption(f"已积累 {len(labels) - n_reject} 次有效选择 + {n_reject} 次否决。")
    if not S.pairs:
        st.info("请先回第①步生成候选对。")
        return
    if S.cursor >= len(S.pairs):
        st.success("这一批候选对已全部投完！可回第①步导入新文本继续，或直接去第③步训练。")
        if st.button("从头再标这一批（重置进度）"):
            S.cursor = 0
            st.rerun()
        return
    st.progress(min(len(labels) / RECOMMEND, 1.0), text=f"标注进度 {len(labels)}/{RECOMMEND}")
    pair = S.pairs[S.cursor]
    st.caption(f"第 {S.cursor + 1}/{len(S.pairs)} 对 · 你的偏好协议：{S.protocol}")
    ca, cb, cc = st.columns(2)
    with ca:
        st.markdown("**候选 甲**")
        st.markdown(pair["cand_a"][:CANDIDATE_MAX_CHARS])
    with cb:
        st.markdown("**候选 乙**")
        st.markdown(pair["cand_b"][:CANDIDATE_MAX_CHARS])

    def _pick(choice: str) -> None:
        row = make_label_row(
            protocol=S.protocol,
            source_id=pair["source_id"],
            cand_a=pair["cand_a"],
            cand_b=pair["cand_b"],
            variant_a=pair["variant_a"],
            variant_b=pair["variant_b"],
            choice=choice,
            labeler="human",
        )
        LABELS.parent.mkdir(parents=True, exist_ok=True)
        with LABELS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        S.cursor += 1
        st.rerun()

    b1, b2, b3 = st.columns([1, 1, 1])
    if b1.button("我选 甲", type="primary", use_container_width=True):
        _pick("甲")
    if b2.button("我选 乙", type="primary", use_container_width=True):
        _pick("乙")
    if b3.button("都不合格", use_container_width=True):
        _pick("REJECT")


# ---- ③ 训练 ----


def step_train():
    st.header("③ 一键训练")
    st.caption("这一步做什么：你的投票自动分成「练习题」和「考试题」（四分之一留作考试，"
               "不参与训练），然后训练一个小判官学会你的口味，约 10~20 分钟。")
    if not torch.cuda.is_available():
        st.warning("未检测到可用 GPU——训练仍可进行，但会慢很多。")
    else:
        st.success(f"GPU 就绪：{torch.cuda.get_device_name(0)}")
    valid = sum(1 for r in _read_labels() if r["choice"] in ("甲", "乙"))
    st.metric("当前有效标注", f"{valid} 对", f"最低 {MIN_PAIRS} · 建议 {RECOMMEND}")
    if ADAPTER.exists():
        st.info("已有训练过的判官：再次训练会覆盖它（先回第②步补充标注更划算）。")
    if valid < MIN_PAIRS:
        st.warning(f"有效标注不足 {MIN_PAIRS} 对，先回第②步继续投票。")
        return
    if st.button("开始训练", type="primary", use_container_width=True):
        st.warning("训练进行中，请不要关闭或刷新页面。")
        rc = _run_with_log(["scripts/build_user_pref_data.py"], TRAIN_LOG, "整理练习题与考试题…")
        if rc != 0:
            st.error("整理数据失败：通常是标注里出现了多种「偏好协议」文字。"
                     "请统一第①步的协议后重试。")
            return
        rc = _run_with_log(
            ["scripts/finetune_judge_dpo.py", "--persona", "USER", "--data", DPO,
             "--out", "models/judge_pref_USER"],
            TRAIN_LOG, "训练中…",
        )
        if rc == 0:
            st.success("训练完成！去第④步看你的判官考了多少分。")
        else:
            st.error(f"训练失败（退出码 {rc}），可按 RUNBOOK 的 θ 段命令在终端重试。")


# ---- ④ 评测 ----


def step_eval():
    st.header("④ 评测出分")
    st.caption("这一步做什么：让你的判官做留出的考题，并让「未训练的通用模型」考同一张卷——"
               "两相对比就是你微调的真实效果。")
    if not (ADAPTER.exists() and Path(REPO / BENCH).exists()):
        st.warning("还没有训练好的判官，先完成第③步。")
        return
    if st.button("开始评测（约 5~10 分钟）", type="primary", use_container_width=True):
        st.warning("评测进行中，请不要关闭或刷新页面。")
        rc = _run_with_log(
            ["scripts/run_pref_benchmark.py", "--benchmark", BENCH,
             "--adapters", "USER=models/judge_pref_USER", "--generic"],
            EVAL_LOG, "评卷中…",
        )
        if rc != 0:
            st.error(f"评测失败（退出码 {rc}）。")
            return
    if REPORT.exists():
        rep = json.loads(REPORT.read_text(encoding="utf-8"))
        judges = rep.get("judges", {})
        mine = judges.get("USER", {}).get("USER/main")
        base = judges.get("generic", {}).get("generic/main")
        if mine is not None and base is not None:
            m1, m2 = st.columns(2)
            m1.metric("通用模型（未微调）", f"{base:.1%}", "猜你的口味 ≈ 瞎猜")
            m2.metric("你的判官", f"{mine:.1%}", f"提升 +{(mine - base) * 100:.0f} 个百分点")
            ctrl_m = judges.get("USER", {}).get("USER/control")
            if ctrl_m is not None:
                st.caption(f"质检题（带广告损伤的候选应被否决）："
                           f"你的判官 {ctrl_m:.1%}（如实记录，不设线）")
        else:
            st.json(judges, expanded=False)
    elif ADAPTER.exists():
        st.info("尚未评测，点上方按钮开始。")


# ---- ⑤ 试用 ----


@st.cache_resource
def _load_judge(adapter_dir: str):
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, torch_dtype=torch.float16 if device == "cuda" else torch.float32
    ).to(device)
    if adapter_dir:
        model = PeftModel.from_pretrained(model, adapter_dir)
    return model.eval(), tok, device


def step_use():
    st.header("⑤ 判官试用")
    st.caption("这一步做什么：贴上任意两个候选，判官按你的偏好替你选。"
               "「候选甲/乙」的顺序不影响结果吗？——会影响一点，这正是它像人的一面。")
    has_adapter = ADAPTER.exists()
    if not has_adapter:
        st.info("还没有训练好的判官：现在作答的是未微调的通用模型，可作对照。")
    ca, cb = st.columns(2)
    a = ca.text_area("候选 甲", height=150)
    b = cb.text_area("候选 乙", height=150)
    if st.button("让判官裁决", type="primary", use_container_width=True):
        if not (a.strip() and b.strip()):
            st.warning("两个候选都要有内容。")
            return
        manifest_p = Path(REPO / BENCH) / "manifest.json"
        protocol = (
            json.loads(manifest_p.read_text(encoding="utf-8"))["personas"]["USER"]
            if manifest_p.exists()
            else S.protocol
        )
        model, tok, device = _load_judge(str(ADAPTER) if has_adapter else "")
        prompt = PREF_PROMPT.format(
            protocol=protocol, a=a[:CANDIDATE_MAX_CHARS], b=b[:CANDIDATE_MAX_CHARS]
        )
        templated = tok.apply_chat_template(
            [{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True
        )
        enc = tok(templated, return_tensors="pt", truncation=True, max_length=704).to(device)
        with torch.no_grad():
            gen = model.generate(
                **enc, max_new_tokens=96, do_sample=False, pad_token_id=tok.pad_token_id
            )
        out = tok.decode(gen[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
        choice = parse_choice(out)
        if choice:
            st.markdown(f"## 判官的选择：**候选 {choice}**")
        else:
            st.error("判官没有给出有效裁决（输出格式异常），重试一次。")
        st.caption(out[:200])


PAGES = {
    "① 导入文本": step_import,
    "② 点击标注": step_annotate,
    "③ 一键训练": step_train,
    "④ 评测出分": step_eval,
    "⑤ 判官试用": step_use,
}

page = st.sidebar.radio("工坊五步", list(PAGES), label_visibility="collapsed")
st.sidebar.caption("偏好判官工坊 · 你的点击 → 你的判官\n\n\ndocs/design_tables.md θ 节")
PAGES[page]()
