"""把官方 pytorch_model.bin 转成 safetensors 本地目录（一次性）。

背景：OFA-Sys/chinese-clip-vit-base-patch16 主分支只有 .bin 权重；新版
transformers 因 CVE-2025-32434 拒绝在 torch<2.6 上 torch.load。为避免
再下 2.5GB 的 torch，把已缓存的官方 bin 用 torch.load(weights_only=True)
读出并转存 safetensors（safetensors 加载路径不受该限制）。

用法：python scripts/convert_clip_weights.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = "OFA-Sys/chinese-clip-vit-base-patch16"
OUT = Path(__file__).resolve().parents[1] / "models" / "chinese-clip-vit-base-patch16"


def find_cached_snapshot() -> Path:
    pattern = Path.home() / ".cache/huggingface/hub" / f"models--{REPO.replace('/', '--')}"
    hits = list(pattern.glob("snapshots/*/pytorch_model.bin"))
    if not hits:
        raise FileNotFoundError(
            f"缓存中未找到 {REPO} 的 pytorch_model.bin，请先运行一次编码器触发下载"
        )
    return hits[0].parent


def main() -> None:
    import torch
    from safetensors.torch import save_model
    from transformers import ChineseCLIPConfig, ChineseCLIPModel

    snap = find_cached_snapshot()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"转换 {snap / 'pytorch_model.bin'} -> {OUT}")

    state = torch.load(snap / "pytorch_model.bin", map_location="cpu", weights_only=True)
    model = ChineseCLIPModel(ChineseCLIPConfig.from_pretrained(snap))
    missing, unexpected = model.load_state_dict(state, strict=False)
    print(f"state_dict: missing={len(missing)}, unexpected={len(unexpected)}")
    if missing:
        print("  missing 示例:", missing[:3])
    save_model(model, str(OUT / "model.safetensors"))
    for extra in ("config.json", "preprocessor_config.json", "vocab.txt"):
        if (snap / extra).exists():
            shutil.copy(snap / extra, OUT / extra)
    print(f"完成: {OUT}")


if __name__ == "__main__":
    main()
