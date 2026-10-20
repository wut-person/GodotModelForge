#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给生成出来的资产打一句描述和标签（调用大模型 API，失败自动降级到规则）。

用法：
    python tools\\pipeline\\asset_caption.py --meta assets\\characters\\demo_ai\\demo_ai.meta.json
    python tools\\pipeline\\asset_caption.py --meta ... --offline        # 只走规则，不联网
    python tools\\pipeline\\asset_caption.py --meta ... --api-key sk-xxx # 临时指定 key

Key 读取顺序：
    1) --api-key
    2) 环境变量 DEEPSEEK_API_KEY
    3) ~/.dsh/.credentials.yaml 里的 refs.DEEPSEEK_API_KEY（本机已有，无需重复配置）

产物：<资产目录>/<资产名>.caption.json
    {
      "asset": "demo_ai",
      "description": "……",
      "tags": ["…"],
      "suggested_name": "…",
      "source": "llm" | "rule",
      "model": "deepseek-chat",
      "elapsed_ms": 1234
    }

设计要点（也是面试可以讲的点）：
    · 模型输出用 response_format=json_object 强约束，程序直接消费，不用人再看
    · 校验失败 / 网络失败 / 没 key 时自动降级到规则生成，保证管线不会因为模型挂了而中断
    · 只读 meta.json（面数/骨骼/动画），不发大文件，控制 token 成本
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
CRED_FILE = os.path.expanduser(r"~\.dsh\.credentials.yaml")

PROMPT_SYS = (
    "你是游戏美术资产管理系统里的命名助手。"
    "根据给定的资产技术参数，输出严格 JSON，不要任何解释文字。"
)

PROMPT_SCHEMA = {
    "description": "一句中文描述，20-40 字，说明这是什么资产、用在什么场景",
    "tags": ["character", "rigged", "lowpoly"],
    "suggested_name": "建议的资产文件名，小写英文加下划线，不超过 3 段",
}


def read_key_from_credentials():
    """从 ~/.dsh/.credentials.yaml 取 DEEPSEEK_API_KEY。

    两种存法都支持：
      · refs 里直接就是 sk-xxx（本机就是这种）
      · refs 指向一条 record 名，secret/token 在 records 段里
    """
    if not os.path.exists(CRED_FILE):
        return None
    try:
        text = open(CRED_FILE, "r", encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"DEEPSEEK_API_KEY\s*:\s*([^\s\"']+)", text)
    if not m:
        return None
    val = m.group(1).strip().strip('"').strip("'")

    # 情况一：refs 里直接放的就是 key
    if val.startswith("sk-") and len(val) >= 20:
        return val

    # 情况二：refs 是记录名，去 records 段找 secret / token
    block = re.search(
        r"^\s{2}" + re.escape(val) + r"\s*:(.*?)(?=^\s{2}[^\s]|\Z)",
        text, re.S | re.M,
    )
    if block:
        for field in ("secret", "token"):
            s = re.search(field + r"\s*:\s*([^\s\"']+)", block.group(1))
            if s:
                return s.group(1).strip().strip('"').strip("'")
    return None


def load_meta(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def summarize(meta):
    """把 meta.json 压成几行给模型看，避免把整份 JSON 都发过去。"""
    bits = []
    if meta.get("generator"):
        bits.append("来源: " + str(meta["generator"]))
    if meta.get("triangles"):
        bits.append("三角面: %s" % meta["triangles"])
    if meta.get("vertices"):
        bits.append("顶点: %s" % meta["vertices"])
    if meta.get("bone_count"):
        bits.append("骨骼: %s 根" % meta["bone_count"])
    if meta.get("clips"):
        bits.append("动画: " + " / ".join(meta["clips"]))
    if meta.get("height_m"):
        bits.append("身高: %.2f m" % meta["height_m"])
    if meta.get("unweighted_vertices") is not None:
        bits.append("未绑定顶点: %s" % meta["unweighted_vertices"])
    if meta.get("prepared") is not None:
        bits.append("是否重拓扑: %s" % ("是" if meta["prepared"] else "否"))
    prep = meta.get("prepare") or {}
    inp = prep.get("input") or {}
    if inp.get("triangles"):
        bits.append("输入面数: %s（AI 生成的三角汤）" % inp["triangles"])
    return "\n".join(bits)


def call_llm(key, model, meta_summary):
    body = {
        "model": model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT_SYS},
            {
                "role": "user",
                "content": (
                    "资产技术参数：\n" + meta_summary +
                    "\n\n请按这个 JSON 结构输出（键名固定）：\n" +
                    json.dumps(PROMPT_SCHEMA, ensure_ascii=False, indent=2)
                ),
            },
        ],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    text = payload["choices"][0]["message"]["content"]
    data = json.loads(text)
    # 校验：三个键都得在，tags 得是列表
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise ValueError("description 缺失")
    tags = data.get("tags")
    # 实测：模型经常把数组写成 "a / b / c" 这种字符串，这里必须容错
    if isinstance(tags, str):
        tags = [p for p in re.split(r"[/,，、|;；\s]+", tags) if p]
    if not isinstance(tags, list) or not tags:
        raise ValueError("tags 缺失")
    data["tags"] = [str(t).strip().lower() for t in tags][:6]
    data["suggested_name"] = str(data.get("suggested_name") or "").strip() or "asset"
    return data


def fallback_rule(meta, asset_name):
    """不联网也要有可用结果：按 meta 里的字段拼一句。"""
    tri = meta.get("triangles")
    bones = meta.get("bone_count")
    clips = meta.get("clips") or []
    parts = []
    if bones:
        parts.append("%d 骨骼" % bones)
    if clips:
        parts.append("%d 段动画" % len(clips))
    if tri:
        parts.append("%d 面" % tri)
    desc = "游戏角色资产"
    if bones:
        desc = "带骨骼动画的类人角色资产"
    if parts:
        desc += "（" + "、".join(parts) + "）"
    tags = ["character"]
    if bones:
        tags.append("rigged")
    if tri and tri <= 12000:
        tags.append("lowpoly")
    if meta.get("prepared"):
        tags.append("retopologized")
    return {
        "description": desc,
        "tags": tags,
        "suggested_name": re.sub(r"[^a-z0-9_]+", "_", asset_name.lower()).strip("_") or "asset",
    }


def main():
    ap = argparse.ArgumentParser(description="给资产生成描述与标签（LLM + 规则降级）")
    ap.add_argument("--meta", required=True, help="资产 meta.json 路径")
    ap.add_argument("--out", default="", help="输出 JSON 路径，默认 <meta 同目录>/<资产名>.caption.json")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--offline", action="store_true", help="不调 API，只用规则")
    args = ap.parse_args()

    meta_path = os.path.abspath(args.meta)
    if not os.path.exists(meta_path):
        print("[caption] 找不到 meta.json: " + meta_path, file=sys.stderr)
        return 2
    meta = load_meta(meta_path)
    asset_dir = os.path.dirname(meta_path)
    asset_name = os.path.splitext(os.path.basename(meta_path))[0]
    if asset_name.endswith(".meta"):          # ai_demo.meta.json -> ai_demo
        asset_name = asset_name[:-5]
    out_path = args.out or os.path.join(asset_dir, asset_name + ".caption.json")

    summary = summarize(meta)
    print("[caption] 资产: " + asset_name)
    print("[caption] 参数摘要:\n" + summary)

    result, source, err = None, "rule", ""
    key = args.api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    if not key and not args.offline:
        key = read_key_from_credentials() or ""

    t0 = time.time()
    if args.offline:
        print("[caption] --offline：跳过 API")
    elif not key:
        print("[caption] 没有可用 API Key，降级到规则生成")
    else:
        try:
            result = call_llm(key, args.model, summary)
            source = "llm"
            print("[caption] 模型返回 OK")
        except urllib.error.HTTPError as e:
            err = "HTTP %s: %s" % (e.code, e.read()[:200].decode("utf-8", "replace"))
            print("[caption] 调用失败，降级: " + err, file=sys.stderr)
        except Exception as e:                      # 网络 / JSON / 校验都兜住
            err = "%s: %s" % (type(e).__name__, e)
            print("[caption] 调用失败，降级: " + err, file=sys.stderr)

    if result is None:
        result = fallback_rule(meta, asset_name)
        source = "rule"

    elapsed_ms = int((time.time() - t0) * 1000)
    payload = {
        "asset": asset_name,
        "description": result.get("description", ""),
        "tags": result.get("tags", []),
        "suggested_name": result.get("suggested_name", ""),
        "source": source,
        "model": args.model if source == "llm" else "",
        "elapsed_ms": elapsed_ms,
    }
    if err:
        payload["fallback_reason"] = err

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("[caption] 写入 " + out_path + "（source=" + source + "，" + str(elapsed_ms) + " ms）")
    print("[caption] " + payload["description"])
    print("[caption] tags: " + ", ".join(payload["tags"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
