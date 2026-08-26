"""Chinese-CLIP 编码器：图像/文本 -> L2 归一化向量。

选型说明（面试考点）：英文 CLIP 对中文 caption 无效（tokenizer 词表缺失 +
中文视觉-语言对齐未训练），所以选 OFA-Sys/chinese-clip-vit-base-patch16
（中文图文对齐的官方开源基座，5 个中文数据集 ~2 亿图文对训练）。

工程细节：
- 惰性加载单例：模型加载 + 权重下载数十秒级，进程内只做一次；
  算子通过 get_encoder() 获取，测试 monkeypatch 同一入口注入假编码器
- 归一化输出：余弦相似度 = 点积，语义去重与检索直接复用
- 国内网络：huggingface_hub 读 HF_ENDPOINT 环境变量走镜像
  （默认已在 download 链路适配 hf-mirror，这里同样遵守）
- 进程内 embedding 缓存：clip_alignment 与 semantic_dedup 共用图像向量，
  避免同批样本重复编码（键为图像路径 + 修改时间）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "OFA-Sys/chinese-clip-vit-base-patch16"
# 官方仓库只有 pytorch_model.bin，新版 transformers 在 torch<2.6 上禁止
# torch.load（CVE-2025-32434）；scripts/convert_clip_weights.py 转出的本地
# safetensors 目录优先（存在即用），否则回落 HF id（在线下载）。
_LOCAL_MODEL = Path(__file__).resolve().parents[3] / "models" / "chinese-clip-vit-base-patch16"
if (_LOCAL_MODEL / "model.safetensors").exists():
    DEFAULT_MODEL = str(_LOCAL_MODEL)
# 镜像在镜像站不可用时回落官方端点
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


class ClipEncoder:
    """图像/文本批量编码。向量均 L2 归一化，点积即余弦相似度。"""

    def __init__(
        self, model_name: str = DEFAULT_MODEL, device: Optional[str] = None, batch_size: int = 64
    ):
        import torch
        from transformers import ChineseCLIPModel, ChineseCLIPProcessor

        self.torch = torch
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.batch_size = batch_size
        logger.info("加载 %s (%s)...", model_name, device)
        self.model = ChineseCLIPModel.from_pretrained(model_name).to(device).eval()
        self.processor = ChineseCLIPProcessor.from_pretrained(model_name)
        self._cache: dict[str, np.ndarray] = {}

    def _norm(self, tensor) -> np.ndarray:
        t = tensor.detach().float().cpu().numpy()
        norms = np.linalg.norm(t, axis=1, keepdims=True)
        return t / np.maximum(norms, 1e-8)

    def encode_images(self, paths: list[str]) -> np.ndarray:
        """按路径批量编码图像，命中缓存的直接复用。返回 (n, d) 归一化矩阵。"""
        keys = [(p, Path(p).stat().st_mtime) for p in paths]
        out: list[Optional[np.ndarray]] = [self._cache.get(f"{p}:{m}") for p, m in keys]
        todo = [i for i, v in enumerate(out) if v is None]
        for start in range(0, len(todo), self.batch_size):
            chunk = todo[start : start + self.batch_size]
            images = [self._load_image(paths[i]) for i in chunk]
            inputs = self.processor(images=images, return_tensors="pt").to(self.device)
            with self.torch.no_grad():
                emb = self.model.get_image_features(**inputs)
            for i, vec in zip(chunk, self._norm(emb)):
                key = f"{keys[i][0]}:{keys[i][1]}"
                self._cache[key] = vec
                out[i] = vec
        return np.stack(out)

    def encode_image_object(self, image) -> np.ndarray:
        """编码单个 PIL Image（查询侧入口：图搜图收 base64/上传对象，
        不落临时文件）。返回 (d,) 归一化向量。"""
        inputs = self.processor(images=[image], return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            emb = self.model.get_image_features(**inputs)
        return self._norm(emb)[0]

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        """批量编码文本。返回 (n, d) 归一化矩阵。"""
        embs = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            inputs = self.processor(
                text=chunk, return_tensors="pt", padding=True, truncation=True, max_length=64
            ).to(self.device)
            with self.torch.no_grad():
                # 不用 model.get_text_features：transformers 4.57 的中文 CLIP 实现
                # 把文本塔建成 add_pooling_layer=False 却读 pooler_output（返回 None，
                # 直接崩溃）；而官方权重本就没有 BERT pooler——正确实现是取 CLS
                # token 的 last_hidden_state 过 text_projection（与 OFA-Sys 官方一致）
                out = self.model.text_model(
                    input_ids=inputs["input_ids"],
                    attention_mask=inputs.get("attention_mask"),
                    token_type_ids=inputs.get("token_type_ids"),
                )
                emb = self.model.text_projection(out.last_hidden_state[:, 0])
            embs.append(self._norm(emb))
        return np.concatenate(embs)

    def _load_image(self, path: str):
        from PIL import Image

        return Image.open(path).convert("RGB")


_ENCODER: Optional[ClipEncoder] = None


def get_encoder() -> ClipEncoder:
    """进程内单例入口（测试用 monkeypatch 替换这里）。"""
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = ClipEncoder()
    return _ENCODER
