# FAQ：真实踩过的坑（全部来自本项目实测，按频次排序）

## 数据与环境

**Q：下载模型/数据报 403 或超时？**
hf-mirror.com 拒绝无 UA 请求（裸 urllib 403），且必须显式走镜像。本项目
下载链路已内建（UA+镜像+重试+幂等）；若用 huggingface_hub，设
`HF_ENDPOINT=https://hf-mirror.com`。

**Q：transformers 报 torch.load CVE 错误？**
transformers 5.x 要求 torch≥2.6；且官方 chinese-clip 仓库只有 .bin。
两条路：降 transformers 4.x + `python scripts/convert_clip_weights.py`
本地转 safetensors（本项目方案），或升 torch≥2.6（2.5GB 下载）。

**Q：CLIP 文本编码崩溃 pooler_output=None？**
transformers 4.57 中文 CLIP 回归 bug。正确实现：CLS token →
text_projection（官方权重本无 pooler）。见 clip_encoder.py 注释。

**Q：Windows 下中文乱码？**
脚本运行加 `python -X utf8`；curl 传中文 JSON 用 `--data-binary @file`。

**Q：numpy 版本冲突？**
`numpy<2` + `opencv-python<5` 同时钉住：torch 2.5 按 numpy 1.x 编译，
opencv 5.x 强制 numpy≥2。requirements.txt 已注明原因。

## 使用

**Q：服务首次请求很慢？**
模型惰性加载（首次 ~8-15s），预热后 67-180ms。生产建议启动时打一次
预热请求（或加 warmup 钩子）。

**Q：CI 里模型相关测试会跑吗？**
FakeEncoder/monkeypatch 全覆盖，CI（CPU、无模型、无数据）自动跳过
3 个数据依赖测试，94+6 项全绿。

**Q：/api/ingest 重启后同样的图又能进来了？**
已知限制：去重状态在内存。重启=重复窗口重开。修复路径见
SLA_README「已知限制」。

**Q：想复现全部实验？**
`make data && make funnel && make index-clean index-dirty && \
python scripts/eval_retrieval.py && make train-detector && make finetune-clip`
约 40 分钟（含 GPU）。

## 评测口径（引用数字前必读）

- 检索对比：D3 报告查询集=漏斗存活集上构造（119 held_out）；消融=全集
  干净子集上构造（120）——口径不同，不能混引（ENGINEERING_NOTES #34）
- 算子 P/R：全量脏集**独立**评测，非漏斗串联（#9/#29）
- 检测器：泛化组（B 组）与训练组字体/文本/透明度错开（#35）
