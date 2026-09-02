"""GPT-2 zh 本地权重保障（safetensors 单一入口）。

背景：uer/gpt2-chinese-cluecorpussmall 只有 .bin 权重，transformers 4.57
因 CVE-2025-32434 在 torch<2.6 上拒绝 torch.load（与 Chinese-CLIP 同一堵墙，
见 docs/ENGINEERING_NOTES.md）。解法同 convert_clip_weights.py：把缓存的
pytorch_model.bin 用 weights_only=True 读出，转存 safetensors 后走
transformers 的 safetensors 加载路径。

ensure_local_gpt2() 是 perplexity 算子与 finetune_gpt2.py 共用的唯一入口：
本地 models/gpt2-chinese-cluecorpussmall 就绪 → 直接用；未就绪 → 从 HF 缓存
转换；缓存也没有 → 报错并给出触发下载的方法。
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = "uer/gpt2-chinese-cluecorpussmall"
OUT = Path(__file__).resolve().parents[2] / "models" / "gpt2-chinese-cluecorpussmall"
_TOKENIZER_FILES = ("vocab.txt", "tokenizer_config.json", "tokenizer.json")

_resolver = None  # 供测试注入，避免单测触碰文件系统


def set_resolver(fn) -> None:
    global _resolver
    _resolver = fn


def _find_cached_snapshot() -> Path:
    pattern = Path.home() / ".cache/huggingface/hub" / f"models--{REPO.replace('/', '--')}"
    hits = sorted(pattern.glob("snapshots/*/pytorch_model.bin"))
    if not hits:
        raise FileNotFoundError(
            f"未找到 {REPO} 的本地缓存。先触发一次下载："
            f'python -c "from huggingface_hub import snapshot_download;'
            f"snapshot_download('{REPO}', endpoint='https://hf-mirror.com')\""
        )
    return hits[0].parent


def ensure_local_gpt2() -> Path:
    """返回含 model.safetensors 的本地模型目录（幂等）。"""
    if _resolver is not None:
        return _resolver()
    if (OUT / "model.safetensors").exists():
        return OUT

    import torch
    from safetensors.torch import save_file
    from transformers import AutoConfig, AutoModelForCausalLM

    snap = _find_cached_snapshot()
    OUT.mkdir(parents=True, exist_ok=True)
    state = torch.load(snap / "pytorch_model.bin", map_location="cpu", weights_only=True)
    model = AutoModelForCausalLM.from_config(AutoConfig.from_pretrained(snap))
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing:
        raise RuntimeError(f"权重转换后缺失 {len(missing)} 个张量，示例: {missing[:3]}")
    # save_model（safetensors 0.7）的 __metadata__ 只写 tied-weight 别名、不含
    # format: pt，transformers 会拒载；必须 save_file + 显式 format。
    # clone 解开共享内存（tied 权重），lm_head 靠 config tie 自动回绑。
    state = {k: v.clone() for k, v in state.items()}
    save_file(state, str(OUT / "model.safetensors"), metadata={"format": "pt"})
    for extra in ("config.json", *_TOKENIZER_FILES):
        if (snap / extra).exists():
            shutil.copy(snap / extra, OUT / extra)
    return OUT
