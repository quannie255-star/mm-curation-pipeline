"""Studio 后端：把「上传 → 检查 → 清洗 → 构建数据集」包成几个 HTTP 调用。

设计原则（决定外行能不能真的用起来）：

1. **前端不碰命令行** —— 所有动作都是 HTTP 接口，浏览器点一下就完事。
2. **每一步都有可读的失败原因** —— 抛 `StudioError` 而不是裸 traceback，
   因为外行看到 `KeyError: 'text'` 只会以为软件坏了。
3. **长任务异步 + 进度可查** —— 清洗可能要几十分钟，不能让浏览器干等超时；
   任务在后台线程跑，前端轮询 `/api/jobs/<id>`。
4. **数据留在本机** —— 上传落到 `data/studio/<会话>/`，只服务localhost。

⚠️ 判据纪律：本模块**不许有「看起来在工作」的假动作**。
`/api/check` 必须真的调`check_upload`（会拒），
`/api/funnel` 必须真的调`run_funnel`（会真耗时）。
相关测试见 `tests/test_studio_backend.py`。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ⚠️ 层级别数：本文件在 src/mm_curation/studio/ 下，仓库根是 **parents[3]**
# （studio → mm_curation → src → <root>）。写成 parents[2] 会把用户数据落到
# **src/data/studio**（源码树里）—— 既污染源码，又会被门禁当成源码目录。
# 实测栽过：目录建到了 src/data 下，删掉才发现。
ROOT = Path(__file__).resolve().parents[3]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))

from mm_curation.studio.checkers import check_upload  # noqa: E402
from mm_curation.studio.recipes import RECIPES, get_recipe  # noqa: E402

LOG = logging.getLogger("studio")
UPLOAD_ROOT = ROOT / "data" / "studio"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
ALLOWED_SUFFIX = {".jsonl", ".json", ".csv", ".tsv", ".txt", ".zip"}
# 与 scripts/build_dataset.py 的 --tokenizer 默认值保持一致（单一真相源在那，
# 这里只是把默认值显式写出，避免用户拿到与命令行不同的分词结果）。
DEFAULT_TOKENIZER = "Qwen/Qwen2.5-0.5B-Instruct"


class StudioError(Exception):
    """面向用户的错误：message 直接显示在页面上，不带 traceback。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# ── 任务表（内存态：单进程服务，重启即丢，对本地工具足够）───────────


@dataclass
class Job:
    id: str
    kind: str  # funnel | dataset
    status: str  # pending | running | done | failed
    stage: str = ""
    progress: float = 0.0
    result: dict[str, Any] | None = None
    error: str = ""
    hint: str = ""
    log: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        if self.status == "running":
            d["elapsed_s"] = round(time.time() - self.started_at, 1)
        return d


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def _new_job(kind: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, status="pending")
    with _LOCK:
        _JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job:
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise StudioError(f"找不到任务 {job_id}", "它可能已过期（服务重启后任务记录会清空）")
    return job


# ── 会话目录 ────────────────────────────────────────────────────────


def session_dir(session: str) -> Path:
    """按会话分目录，避免不同用户的文件互相覆盖。

    session 只允许安全字符 —— 它会拼进文件系统路径，
    不校验就是路径穿越漏洞（`../../`）。
    """
    if (
        not session
        or any(ch in session for ch in "/\\..")
        or not session.replace("-", "").replace("_", "").isalnum()
    ):
        raise StudioError("非法的会话标识", "请重新打开页面")
    d = UPLOAD_ROOT / session
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 接口实现 ────────────────────────────────────────────────────────


def list_scenarios() -> list[dict]:
    """给前端渲染场景卡。附带算子的中文说明与代价档（现取注册表）。"""
    from curation_eval.registry import available_operator_metas

    import mm_curation.operators  # noqa: F401  触发注册

    from .recipes import COST_ZH, OPERATOR_ZH

    metas = available_operator_metas()
    out = []
    for r in RECIPES:
        steps = []
        for op, params in r.operators:
            m = metas.get(op)
            cost = m.cost_class if m else "rule"
            cost_key = getattr(cost, "value", cost)
            steps.append(
                {
                    "op": op,
                    # ⭐ 外行看不懂 `text_minhash` —— 必须带中文说明。
                    # 自检（assert_recipes_valid）保证每个算子都有，缺了就响亮失败。
                    "label": OPERATOR_ZH.get(op, f"（缺中文说明：{op}）"),
                    "params": params,
                    "cost": cost_key,
                    "cost_zh": COST_ZH.get(cost_key, cost_key),
                    "modality_ok": (not m.modalities) or (r.modality in m.modalities),
                    "shardable": m.shardable if m else True,
                }
            )
        out.append(
            {
                "key": r.key,
                "title": r.title,
                "modality": r.modality,
                "blurb": r.blurb,
                "required_fields": list(r.required_fields),
                "optional_fields": list(r.optional_fields),
                "produces": r.produces,
                "sample_hint": r.sample_hint,
                "cost_note": r.cost_note,
                "caveats": list(r.caveats),
                "steps": steps,
                "n_steps": len(steps),
            }
        )
    return out


def do_check(session: str, filename: str, scenario_key: str, images_uploaded: bool) -> dict:
    """真跑`check_upload`。有错就抛 StudioError（前端红字显示）。"""
    recipe = get_recipe(scenario_key)
    path = session_dir(session) / filename
    if not path.exists():
        raise StudioError(f"没找到文件 {filename}", "请重新上传")
    image_root = None
    if "image_path" in recipe.required_fields:
        if not images_uploaded:
            raise StudioError(
                "这个场景需要图片",
                f"你的数据里有 image_path 字段，请把图片打包成 zip 上传（{recipe.sample_hint}）",
            )
        image_root = session_dir(session) / "images"
    res = check_upload(
        path,
        required_fields=recipe.required_fields,
        optional_fields=recipe.optional_fields,
        image_root=image_root,
    )
    if not res.ok:
        raise StudioError(res.fatal, "请按提示修正文件后重新上传")
    return {
        "ok": True,
        "n_rows": res.n_rows,
        "n_bad_json": res.n_bad_json,
        "notes": res.notes,
        "scenario": scenario_key,
    }


def start_funnel(session: str, filename: str, scenario_key: str, limit: int = 0) -> dict:
    """后台跑漏斗，立刻返回 job_id。"""
    recipe = get_recipe(scenario_key)
    src = session_dir(session) / filename
    if not src.exists():
        raise StudioError(f"没找到文件 {filename}", "请重新上传")
    out_dir = session_dir(session) / "cleaned"
    spec = recipe.spec_to_yaml(str(src), str(out_dir))
    if limit:
        spec["dataset"]["limit"] = limit

    job = _new_job("funnel")
    threading.Thread(target=_run_funnel, args=(job, spec, recipe.key), daemon=True).start()
    return job.as_dict()


def _run_funnel(job: Job, spec: dict, scenario_key: str) -> None:
    from mm_curation.operators.base import Sample
    from mm_curation.pipeline import PipelineConfig, run_funnel
    from mm_curation.pipeline.config import OperatorSpec

    job.status = "running"
    job.started_at = time.time()
    try:
        cfg = PipelineConfig(
            name=spec["name"],
            raw_jsonl=Path(spec["dataset"]["raw_jsonl"]),
            output_dir=Path(spec["output"]["dir"]),
            operators=[
                OperatorSpec(op=o, params=p)
                for o, p in [(x["op"], x.get("params", {})) for x in spec["operators"]]
            ],
            description=spec.get("description", ""),
        )
        limit = int(spec["dataset"].get("limit") or 0)
        rows: list[Sample] = []
        bad = 0
        bad_examples: list[str] = []
        n_autoid = 0
        for i, line in enumerate(open(cfg.raw_jsonl, encoding="utf-8-sig")):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                if len(bad_examples) < 3:
                    bad_examples.append(f"第 {i + 1} 行不是合法 JSON")
                continue
            # ⚠️ `id` 是 Sample 的**协议必填字段**，而用户的数据几乎不会自带。
            #    不补的话每条都会`missing 1 required positional argument: 'id'`
            #    —— 而且这个错会表现为「读入条数比文件行数少」，
            #    用户完全看不出是自己漏了字段。所以这里按行号自动补。
            if not str(rec.get("id") or "").strip():
                rec["id"] = f"row{i + 1}"
                n_autoid += 1
            try:
                rows.append(Sample.from_dict(rec))
            except Exception as e:  # noqa: BLE001
                bad += 1
                if len(bad_examples) < 3:
                    bad_examples.append(f"第 {i + 1} 行：{type(e).__name__}")
            # 试跑条数：**读够就停**（放在 add 之后算，最后一行算超了也无妨）
            if limit and len(rows) >= limit:
                break
        job.log.append(f"读入 {len(rows)} 条样本（跳过 {bad} 行无法解析）")
        if n_autoid:
            job.log.append(f"其中 {n_autoid} 条没有 id 字段，已按行号自动补上")
        if bad_examples:
            job.log.append("跳过的行示例：" + "；".join(bad_examples))
        job.stage = "清洗中"
        job.progress = 0.1

        result = run_funnel(rows, cfg)
        job.progress = 0.85

        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.output_dir / "cleaned.jsonl", "w", encoding="utf-8") as f:
            for s in result.kept:
                f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
        with open(cfg.output_dir / "dropped.jsonl", "w", encoding="utf-8") as f:
            for stage, s in result.dropped:
                row = s.to_dict()
                row["dropped_by"] = stage
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        from mm_curation.quality import build_report_data

        report = build_report_data(result, cfg.name, len(rows))
        (cfg.output_dir / "funnel_stats.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        job.result = {
            "n_input": len(rows),
            "n_kept": len(result.kept),
            "n_dropped": len(result.dropped),
            "keep_rate": (len(result.kept) / len(rows)) if rows else 0.0,
            "stages": [
                {
                    "op": st.op,
                    "n_in": st.n_in,
                    "n_out": st.n_out,
                    "dropped": st.dropped,
                    "pass_rate": st.pass_rate,
                }
                for st in result.stats
            ],
            "cleaned_path": str(cfg.output_dir / "cleaned.jsonl"),
            "dropped_path": str(cfg.output_dir / "dropped.jsonl"),
            "scenario": scenario_key,
        }
        job.status = "done"
        job.progress = 1.0
        job.stage = "完成"
    except Exception as e:  # noqa: BLE001
        LOG.error("漏斗失败: %s", traceback.format_exc())
        job.status = "failed"
        job.stage = "失败"
        job.error = f"{type(e).__name__}: {e}"
        job.hint = _friendly_hint(e)
        job.log.append("失败：" + job.error)
    finally:
        job.finished_at = time.time()


_FRIENDLY = (
    ("CUDA out of memory", "显存不够。关掉其它占显存的程序，或先用「试跑条数」跑一小批。"),
    ("out of memory", "内存不够。先用「试跑条数」限制在几百条。"),
    ("FileNotFoundError", "文件路径不对 —— 如果数据里有 image_path，请确认图片 zip 也上传了。"),
    ("KeyError", "数据里缺少某个字段。对照上面的「格式要求」检查列名是否一致。"),
    ("UnrecognizedArgument", "某个算子的参数名不对 —— 这是系统配置问题，请反馈。"),
    ("ModuleNotFoundError", "缺少 Python 依赖。按项目说明装依赖后重试。"),
    ("ConnectionError", "模型下载失败。检查网络，或先用不依赖大模型的场景（如纯文本规则档）。"),
)


def _friendly_hint(exc: Exception) -> str:
    s = f"{type(exc).__name__}: {exc}"
    for key, hint in _FRIENDLY:
        if key.lower() in s.lower():
            return hint
    return ""


def start_dataset(
    session: str,
    dataset_name: str,
    pack_block_size: int = 512,
    val_ratio: float = 0.1,
    test_ratio: float = 0.1,
    max_tokens: int = 4096,
) -> dict:
    """把清洗产物打包成可训练数据集（调既有 DatasetBuilder）。"""
    cleaned = session_dir(session) / "cleaned" / "cleaned.jsonl"
    if not cleaned.exists():
        raise StudioError("还没有清洗结果", "请先跑完清洗步骤")
    if not dataset_name or not all(ch.isalnum() or ch in "-_" for ch in dataset_name):
        raise StudioError(
            "数据集名只能用字母、数字、连字符、下划线",
            f"当前输入：{dataset_name!r}",
        )
    job = _new_job("dataset")
    threading.Thread(
        target=_run_dataset,
        args=(job, cleaned, dataset_name, pack_block_size, val_ratio, test_ratio, max_tokens),
        daemon=True,
    ).start()
    return job.as_dict()


def _run_dataset(
    job: Job, cleaned: Path, name: str, pack: int, val_r: float, test_r: float, max_tok: int
) -> None:
    """走 `DatasetBuilder` 的真实序列：构造 → add(...) 逐条 → finalize()。

    ⚠️ 不要凭记忆写签名 —— 这里调用的每个参数名都对着
    `src/mm_curation/dataset/build.py` 的 `__init__` 核过。
    （第一版我写成了 `mod.build_dataset(...)`，而那个函数**根本不存在**：
    `scripts/build_dataset.py` 只有 `main()`，可复用类是 `DatasetBuilder`。
    这种「猜一个看起来合理的 API」必然翻车。）
    """
    job.status = "running"
    job.started_at = time.time()
    job.stage = "加载分词器"
    job.progress = 0.05
    try:
        from transformers import AutoTokenizer

        from mm_curation.dataset import DatasetBuilder, iter_jsonl

        tok = AutoTokenizer.from_pretrained(DEFAULT_TOKENIZER)
        job.stage = "打包与切分"
        job.progress = 0.25
        out_dir = UPLOAD_ROOT / "datasets" / name
        builder = DatasetBuilder(
            name,
            tok,
            out_dir=out_dir,
            tokenizer_name=DEFAULT_TOKENIZER,
            source_files=[str(cleaned)],
            funnel_config="",
            funnel_ops=[],
            license_="unknown",
            notes=f"Studio 会话 {cleaned.parent.parent.name}",
            shard_rows=2000,
            val_ratio=val_r,
            test_ratio=test_r,
            split_key=None,
            max_tokens_per_sample=max_tok or None,
            pack_block_size=pack or None,
        )
        n_in = 0
        for rec in iter_jsonl(cleaned):
            rec.setdefault("source", cleaned.name)
            builder.add(rec)
            n_in += 1
            if n_in % 500 == 0:
                job.progress = min(0.85, 0.25 + n_in / 5000)
                job.log.append(f"已处理 {n_in} 条")
        job.stage = "写盘与校验"
        job.progress = 0.9
        builder.finalize()
        man = builder.manifest
        if man is None:
            raise RuntimeError("构建结束但 manifest 为空 —— 这不该发生，请反馈")
        job.result = {
            "dataset_root": str(out_dir),
            "n_samples": man.n_samples,
            "n_tokens": man.n_tokens,
            "n_blocks": getattr(man, "n_blocks", None),
            "splits": man.splits,
            "n_shards": man.n_shards,
            "n_truncated": man.n_truncated,
            "leakage_clean": man.leakage_check.get("clean"),
            "pack_block_size": man.pack_block_size,
        }
        job.status = "done"
        job.progress = 1.0
        job.stage = "完成"
    except Exception as e:  # noqa: BLE001
        LOG.error("构建数据集失败: %s", traceback.format_exc())
        job.status = "failed"
        job.stage = "失败"
        job.error = f"{type(e).__name__}: {e}"
        job.hint = _friendly_hint(e)
        job.log.append("失败：" + job.error)
    finally:
        job.finished_at = time.time()


def session_of(path: Path) -> str:
    """从 datasets/<session>/cleaned/cleaned.jsonl 反推会话 id。"""
    return path.parent.parent.name
