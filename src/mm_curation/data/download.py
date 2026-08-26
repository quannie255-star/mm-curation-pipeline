"""种子数据集下载与格式统一：COCO-CN 标注 + HF 镜像原始 JPEG。

数据源选型记录（2026-08-20 实测，防止后人踩坑，详见 ROADMAP）：
- justram/COCO2014-Images（parquet）：无文件名字段，image_id 是自增序号而非
  COCO 官方 id，无法与 COCO-CN 标注对齐 → 弃用
- ali-sh07/COCO-train2014：~10k 张原始分辨率 JPEG，保留 COCO 原始文件名，
  与 COCO-CN 随机交集 ~16%（1,620 对）→ 当前种子集
- 全量 20,341 对的扩展路径：叠加其他 train2014 镜像（HF 搜索
  coco_train2014 有多个候选），list_remote_files 支持多 repo 合并

工程细节：
- hf-mirror.com 拒绝无 UA 请求（403），所有请求必须带浏览器 UA
- 下载幂等：已存在且非空的文件跳过；失败重试
- 小文件并发下载（线程池），避免上千个串行请求
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .sources import COCO_CN_TAR_URL, CocoCnAnnotations, parse_coco_cn_tar

logger = logging.getLogger(__name__)

DEFAULT_MIRROR = "https://hf-mirror.com"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) mm-curation/0.1"
IMAGE_REPO = "ali-sh07/COCO-train2014"


def _get_json(url: str, retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logger.warning("请求失败(%s/%s) %s: %s", attempt, retries, url, e)
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def fetch(url: str, dest: Path, *, retries: int = 3) -> Path:
    """下载单文件到 dest（带 UA + 重试）；已存在且非空则跳过。"""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            part = dest.with_suffix(dest.suffix + ".part")
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                f.write(r.read())
            part.replace(dest)  # 原子改名：磁盘上不会出现半截文件
            return dest
        except (urllib.error.URLError, OSError) as e:
            logger.warning("下载失败(%s/%s) %s: %s", attempt, retries, url, e)
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def list_remote_files(repo: str, mirror: str = DEFAULT_MIRROR) -> set[str]:
    """列出 HF 数据集仓库的全部文件名。"""
    info = _get_json(f"{mirror}/api/datasets/{repo}")
    return {s["rfilename"] for s in info["siblings"] if s["rfilename"]}


def download_matched_images(
    filenames: list[str],
    images_dir: Path,
    repo: str = IMAGE_REPO,
    mirror: str = DEFAULT_MIRROR,
    workers: int = 8,
) -> tuple[int, int]:
    """并发下载文件名列表中的 JPEG，返回 (成功数, 失败数)。"""
    images_dir.mkdir(parents=True, exist_ok=True)
    todo = [n for n in filenames if not (images_dir / n).exists()]
    if not todo:
        return 0, 0
    ok, fail = 0, 0

    def _one(name: str) -> None:
        url = f"{mirror}/datasets/{repo}/resolve/main/{name}"
        fetch(url, images_dir / name)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, n): n for n in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                fut.result()
                ok += 1
            except Exception as e:  # 单文件失败不阻塞整体，最后汇总
                fail += 1
                logger.warning("跳过 %s: %s", futures[fut], e)
            if i % 200 == 0:
                logger.info("下载进度: %s/%s", i, len(todo))
    return ok, fail


def emit_samples(annotations: CocoCnAnnotations, images_dir: Path, out_jsonl: Path) -> dict:
    """把已下载图像 + 标注统一为 samples.jsonl（与 operators.base.Sample 对齐）。"""
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    with open(out_jsonl, "w", encoding="utf-8") as out:
        for name in annotations.image_names():
            img = images_dir / f"{name}.jpg"
            if not img.exists():
                continue
            captions = annotations.captions.get(name, [])
            if not captions:
                continue
            sample = {
                "id": name,
                "image_path": str(img),
                "caption": captions[0],  # 一图多句时取首句，其余进 meta
                "meta": {
                    "extra_captions": captions[1:],
                    "tags": annotations.tags.get(name, []),
                    "split": annotations.splits[name],
                    "source": "COCO-CN",
                },
                "labels": {},
            }
            out.write(json.dumps(sample, ensure_ascii=False) + "\n")
            kept += 1
    logger.info("emit 完成: %s 条样本 -> %s", kept, out_jsonl)
    return {"kept": kept}


def download_seed_dataset(
    out_dir: str | Path = "data/raw",
    mirror: str = DEFAULT_MIRROR,
    repo: str = IMAGE_REPO,
    limit: int | None = None,
    workers: int = 8,
) -> Path:
    """一键产出 data/raw/samples.jsonl（脏数据种子集的干净底座）。"""
    out_dir = Path(out_dir)
    cache = out_dir / ".cache"

    tar = cache / "coco-cn.tar.gz"
    if not (tar.exists() and tar.stat().st_size > 10_000_000):  # 完整包 ~15.4MB
        fetch(COCO_CN_TAR_URL.replace("https://hf-mirror.com", mirror), tar)
    annotations = parse_coco_cn_tar(tar)
    logger.info("COCO-CN 标注: %s 张图", len(annotations))

    remote = list_remote_files(repo, mirror)
    wanted = sorted(f"{n}.jpg" for n in annotations.splits if f"{n}.jpg" in remote)
    if limit:
        wanted = wanted[:limit]
    logger.info("镜像可命中: %s 张（repo=%s）", len(wanted), repo)
    ok, fail = download_matched_images(wanted, out_dir / "images", repo, mirror, workers)

    stats = emit_samples(annotations, out_dir / "images", out_dir / "samples.jsonl")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "dataset": "COCO-CN + ali-sh07/COCO-train2014",
                "annotations": len(annotations),
                "matched": len(wanted),
                "downloaded": ok,
                "download_failed": fail,
                "kept": stats["kept"],
                "mirror": mirror,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_dir / "samples.jsonl"
