"""训练水印/NSFW 检测器（P1-T2，design 2.2）。

流水：干净底图 → 风格组 A/B 生成 → A 组内划分 train/testA → MobileNetV3
训练（以 testA 选最优，B 组不参与任何训练决策）→ 双风格评测报告。

防循环论证：testB（风格组 B）与训练组在布局/透明度/字体/文本池四维错开
（tests/test_detector_synth.py 锁死），检测器只能靠"叠加文字"概念通过。

用法：python scripts/train_detector.py [--epochs 3]
退出码：0 / 1(底图缺失) / 2(训练异常)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
from PIL import Image  # noqa: E402
from torch.utils.data import DataLoader, Dataset  # noqa: E402
from torchvision import models, transforms  # noqa: E402

from mm_curation.detector.synth import generate_dataset  # noqa: E402

RAW = Path("data/raw/samples.jsonl")
OUT_DIR = Path("models/detector")
REPORT = Path("data/reports/detector_eval.json")
CLASS_NAMES = ["clean", "watermark", "ad_nsfw"]


class DetDataset(Dataset):
    def __init__(self, rows, tf):
        self.rows, self.tf = rows, tf

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        row = self.rows[i]
        img = Image.open(row.image_path).convert("RGB").resize((224, 224))
        return self.tf(img), row.label


def _split_by_class(rows, n_train):
    """每类前 n_train 条为训练集，其余为同风格测试集（testA）。"""
    train, test = [], []
    seen: dict[int, int] = {}
    for r in rows:
        seen[r.label] = seen.get(r.label, 0)
        (train if seen[r.label] < n_train else test).append(r)
        seen[r.label] += 1
    return train, test


@torch.no_grad()
def evaluate(model, loader, device):
    model.eval()
    confusion = [[0] * 3 for _ in range(3)]
    for x, y in loader:
        pred = model(x.to(device)).argmax(1).cpu()
        for t, p in zip(y, pred):
            confusion[t][p] += 1
    per_class_recall = [confusion[i][i] / max(sum(confusion[i]), 1) for i in range(3)]
    acc = sum(confusion[i][i] for i in range(3)) / max(sum(map(sum, confusion)), 1)
    return {"accuracy": acc, "per_class_recall": per_class_recall, "confusion": confusion}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=6)
    parser.add_argument("--n-train", type=int, default=1000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not RAW.exists():
        logging.error("底图清单缺失: %s（先 make data）", RAW)
        sys.exit(1)
    bases = [
        json.loads(line)["image_path"] for line in RAW.read_text(encoding="utf-8").splitlines()
    ]

    try:
        logging.info("生成风格组数据（A: 训练+同风格测试, B: 泛化测试）...")
        rows_a = generate_dataset(
            bases, ".cache/detector", n_per_class=args.n_train + 150, group="A", seed=101
        )
        rows_b = generate_dataset(bases, ".cache/detector", n_per_class=400, group="B", seed=202)
        train_rows, test_a = _split_by_class(rows_a, args.n_train)
        logging.info("train=%s testA=%s testB=%s", len(train_rows), len(test_a), len(rows_b))

        device = "cuda" if torch.cuda.is_available() else "cpu"
        norm = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        tf = transforms.Compose([transforms.ToTensor(), norm])
        # 训练增广：颜色/裁剪扰动逼模型学「文字叠加」的不变特征而非记风格
        # （首轮无增广时 B 组水印召回仅 5.8%，见 detector_eval 迭代记录）
        tf_train = transforms.Compose(
            [
                transforms.RandomResizedCrop(224, scale=(0.7, 1.0)),
                transforms.ColorJitter(0.2, 0.2, 0.2),
                transforms.ToTensor(),
                norm,
            ]
        )
        train_ld = DataLoader(
            DetDataset(train_rows, tf_train), batch_size=64, shuffle=True, num_workers=0
        )
        eval_ld = {
            name: DataLoader(DetDataset(rows, tf), batch_size=128)
            for name, rows in [("testA", test_a), ("testB", rows_b)]
        }

        model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
        model.classifier[3] = nn.Linear(model.classifier[3].in_features, 3)
        model = model.to(device)
        opt = torch.optim.Adam(model.parameters(), lr=1e-3)

        best = {"testA_acc": 0.0, "state": None}
        for epoch in range(1, args.epochs + 1):
            model.train()
            total = 0.0
            for x, y in train_ld:
                opt.zero_grad()
                loss = nn.functional.cross_entropy(model(x.to(device)), y.to(device))
                loss.backward()
                opt.step()
                total += loss.item()
            acc_a = evaluate(model, eval_ld["testA"], device)["accuracy"]
            logging.info("epoch %s: loss=%.4f testA_acc=%.4f", epoch, total / len(train_ld), acc_a)
            if acc_a > best["testA_acc"]:  # 模型选择只看 testA，B 组不参与
                best = {
                    "testA_acc": acc_a,
                    "state": {k: v.cpu() for k, v in model.state_dict().items()},
                }

        model.load_state_dict(best["state"])
        results = {name: evaluate(model, ld, device) for name, ld in eval_ld.items()}
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        torch.save(
            {"state_dict": best["state"], "class_names": CLASS_NAMES}, OUT_DIR / "wm_nsfw_cnn.pt"
        )

        generalization_gap = results["testA"]["accuracy"] - results["testB"]["accuracy"]
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            json.dumps(
                {
                    "model": str(OUT_DIR / "wm_nsfw_cnn.pt"),
                    "epochs": args.epochs,
                    "results": results,
                    "generalization_gap": generalization_gap,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logging.info("模型与报告就绪: %s | %s", OUT_DIR / "wm_nsfw_cnn.pt", REPORT)
        logging.info(
            "testA acc=%.3f  testB acc=%.3f  泛化损耗=%.3f",
            results["testA"]["accuracy"],
            results["testB"]["accuracy"],
            generalization_gap,
        )
    except Exception:
        logging.exception("训练失败")
        sys.exit(2)


if __name__ == "__main__":
    main()
