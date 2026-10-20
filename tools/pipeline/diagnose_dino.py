"""
诊断：检查 DINO 图像编码器对参考图的输出是否数值健康。

【为什么需要查这个】
图生3D 出来的网格是一块「2m × 2m × 0.37m 的平板」——这是典型的**条件特征退化**：
DiT 没拿到有效的图像信息，只能吐出先验均值（扁平块）。
DINOv3-giant 那个量级的编码器（40 层 × 1536 维）如果以 fp16 跑，
激活值很容易溢出成 inf/nan；一旦条件里出现非有限值，几何就会塌。

本脚本对比 fp16 / fp32 两种精度下编码器输出的统计量，
用来判定「是精度问题」还是「模型/权重本身有问题」。
"""

import os
import sys
import warnings

import numpy as np
import torch

warnings.filterwarnings("ignore")

sys.path.insert(0, r"D:\Tools\Hunyuan3D-2")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def stats(name, t):
    if t is None:
        print("  %-22s None" % name)
        return
    a = t.detach().float()
    finite = torch.isfinite(a)
    n_bad = int((~finite).sum())
    print("  %-22s shape=%s dtype=%s" % (name, tuple(a.shape), t.dtype))
    print("  %-22s 非有限值=%d  min=%.3f max=%.3f mean=%.4f std=%.4f"
          % ("", n_bad,
             float(a[finite].min()) if finite.any() else float("nan"),
             float(a[finite].max()) if finite.any() else float("nan"),
             float(a[finite].mean()) if finite.any() else float("nan"),
             float(a[finite].std()) if finite.any() else float("nan")))


def main():
    img_path = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\deepseek Flash\GodotModelForge\tools\_fixtures\refs\hero_ref.png"

    import yaml
    from PIL import Image
    from transformers import Dinov2WithRegistersConfig, Dinov2WithRegistersModel
    from safetensors import safe_open
    import dino_key_map

    cfg = yaml.safe_load(open(r"D:\Tools\models\hunyuan3d-2mini\hunyuan3d-dit-v2-mini-turbo\config.yaml",
                              encoding="utf-8"))
    dino_cfg = cfg["conditioner"]["params"]["main_image_encoder"]["kwargs"]["config"]

    print("=== 构建并加载 DINO 编码器 ===")
    base = dino_key_map.build_dino_from_config(dino_cfg, verbose=False)
    ckpt = r"D:\Tools\models\hunyuan3d-2mini\hunyuan3d-dit-v2-mini-turbo\model.fp16.safetensors"
    with safe_open(ckpt, framework="pt") as f:
        cond = {k: f.get_tensor(k) for k in f.keys() if k.startswith("conditioner.")}
    st = dino_key_map.load_into(base, cond, verbose=False)
    print("  加载 %d/%d 张量, 权重 std=%.5f" % (st["loaded"], st["model_keys"], st["weight_std_avg"]))

    image = Image.open(img_path).convert("RGB")
    print("  输入图: %s %s" % (img_path, image.size))

    # 预处理：与 conditioner.py 里的 transform 保持一致
    size = int(dino_cfg.get("image_size", 1022))
    from torchvision import transforms
    tf = transforms.Compose([
        transforms.Resize(size, transforms.InterpolationMode.BILINEAR, antialias=True),
        transforms.CenterCrop(size),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    x = tf(image).unsqueeze(0)
    print("  预处理后: %s  范围 [%.3f, %.3f]" % (tuple(x.shape), float(x.min()), float(x.max())))

    print("\n=== 编码器输出对比 ===")
    for dtype, tag in ((torch.float32, "fp32"), (torch.float16, "fp16")):
        m = base.to(dtype=dtype).eval()
        inp = x.to(dtype=dtype)
        with torch.no_grad():
            out = m(inp, output_hidden_states=True)   # Dinov2WithRegistersModel 返回带 last_hidden_state
            feat = out.last_hidden_state if hasattr(out, "last_hidden_state") else out
        print("\n--- %s ---" % tag)
        print("  输入:")
        stats("  输入", inp)
        print("  输出:")
        stats("  last_hidden_state", feat)
        # 每层范数，定位是从哪一层开始爆的。
        # 用模型自己吐的 hidden_states；不要手工把原图喂给每一层——shape 不匹配，
        # 之前就是这里抛 "expected input with shape [*, 1536], but got [1, 3, 518, 518]"。
        hs = getattr(out, "hidden_states", None) or ()
        norms = []
        for i, h in enumerate(hs):
            h = h.detach()
            norms.append((i, float(h.float().abs().max()), int((~torch.isfinite(h)).sum())))
        bad = [n for n in norms if n[2] > 0]
        if norms:
            print("  逐层最大绝对值: 首=%.2f 末=%.2f" % (norms[0][1], norms[-1][1]))
        else:
            print("  未取到 hidden_states，跳过逐层检查")
        if bad:
            print("  ❌ 从第 %d 层开始出现非有限值（共 %d 层受影响）" % (bad[0][0], len(bad)))
        else:
            print("  ✅ 所有层数值有限")

    del m
    torch.cuda.empty_cache()
    print("\n=== 结论提示 ===")
    print("  若 fp16 出现非有限值而 fp32 正常 → 改用 fp32 跑编码器")
    print("  若两者都正常 → 问题在 DiT/VAE 环节或输入图本身")


if __name__ == "__main__":
    main()
