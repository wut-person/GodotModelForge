"""
下载 Hunyuan3D-2mini 的「形状生成」权重（本地免费图生3D 的唯一可行选择）。

为什么是它：
  - 官方 README 明确写「shape generation 6 GB VRAM」，是本机 6 GB 显存下唯一装得下的生成模型
  - 实测权重：turbo DiT 3.56 GB + VAE 0.38 GB ≈ 3.9 GB，剩余显存够跑推理
  - TRELLIS.2 实测 int8 权重就要 4.89 GB，加 VAE/CLIP 共约 7.9 GB，6 GB 装不下

网络：huggingface.co 在本机超时，必须走 hf-mirror.com（通过官方 HF_ENDPOINT 环境变量）。
"""

import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from huggingface_hub import snapshot_download  # noqa: E402

REPO = "tencent/Hunyuan3D-2mini"
DEST = os.environ.get("MODELS_DIR", r"D:\Tools\models")

# 只要形状生成相关的部分；纹理相关的（withencoder 等）体积大且 6 GB 跑不动
ALLOW = [
    "hunyuan3d-dit-v2-mini-turbo/*",
    "hunyuan3d-vae-v2-mini-turbo/*",
    "hunyuan3d-vae-v2-mini/*",
    "*.json",
    "*.yaml",
    "*.txt",
    "NOTICE",
]
IGNORE = ["*withencoder*", "*.ckpt", "*.bin", "*.msgpack", "*.h5"]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def main():
    print("[download] 仓库: %s" % REPO)
    print("[download] 镜像: %s" % os.environ["HF_ENDPOINT"])
    print("[download] 目标: %s" % DEST)
    t0 = time.time()
    path = snapshot_download(
        repo_id=REPO,
        local_dir=os.path.join(DEST, "hunyuan3d-2mini"),
        allow_patterns=ALLOW,
        ignore_patterns=IGNORE,
        max_workers=4,
    )
    dt = time.time() - t0

    total = 0
    print("\n[download] 完成，用时 %.0f 秒 (%.1f MB/s)" % (dt, 0))
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            p = os.path.join(root, f)
            sz = os.path.getsize(p)
            total += sz
            rel = os.path.relpath(p, path)
            print("  %-62s %10s" % (rel, human(sz)))
    print("[download] 合计 %s -> %s" % (human(total), path))


if __name__ == "__main__":
    sys.exit(main())
