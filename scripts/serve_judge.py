"""本地 LLM 判官服务：极简 OpenAI 兼容层（Windows 可跑，vLLM 的本机替代）。

只实现 judge 算子需要的最小协议面（/v1/chat/completions + /v1/models）：
- Linux/多卡机器换 vLLM：`vllm serve <model> --port 8100`，算子零改动
- 云端 API：算子 base_url/api_key 指过去即可
- 默认模型 Qwen2.5-0.5B-Instruct（~1GB，safetensors，8GB 显存安全）

用法：python -X utf8 scripts/serve_judge.py [--model Qwen/Qwen2.5-0.5B-Instruct]
      [--port 8100]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

app = FastAPI(title="local-judge")
_state: dict = {}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "judge"
    messages: list[ChatMessage]
    max_tokens: int = 64
    temperature: float = 0.0


def load_model(name: str):
    tok = AutoTokenizer.from_pretrained(name)
    model = AutoModelForCausalLM.from_pretrained(name)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device).eval()
    if tok.pad_token_id is None:
        tok.pad_token_id = tok.eos_token_id
    return model, tok, device


@app.get("/v1/models")
def models():
    return {"data": [{"id": _state.get("model_name", "judge"), "object": "model"}]}


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    model, tok, device = _state["model"], _state["tok"], _state["device"]
    prompt = req.messages[-1].content if req.messages else ""
    # 必须套 chat template：instruct 模型不进模板就不遵循「只输出 JSON」
    # 类指令（首跑实测 78% 解析失败），且不吐 EOS 拖满 max_new_tokens
    text = tok.apply_chat_template(
        [{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True
    )
    inputs = tok(text, return_tensors="pt", truncation=True, max_length=1024).to(device)
    t0 = time.perf_counter()
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max(8, min(req.max_tokens, 128)),
            do_sample=req.temperature > 0,
            pad_token_id=tok.pad_token_id,
        )
    content = tok.decode(out[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True).strip()
    logging.info(
        "judge %sms: %s", int((time.perf_counter() - t0) * 1000), content[:60].replace("\n", " ")
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": int(inputs["input_ids"].shape[1]),
            "completion_tokens": int(out.shape[1] - inputs["input_ids"].shape[1]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    logging.info("加载 %s（首次会经 hf-mirror 下载 ~1GB）…", args.model)
    model, tok, device = load_model(args.model)
    _state.update(model=model, tok=tok, device=device, model_name=args.model)
    logging.info("就绪（device=%s），监听 %s:%s", device, args.host, args.port)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
