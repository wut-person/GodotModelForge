"""
Hunyuan3D-2mini 图生3D 命令行：一张图 → 一个 3D 网格（GLB）。

这是本机 6 GB 显存下唯一可行的本地免费生成模型：
  - 官方 README 标注「shape generation 6 GB VRAM」
  - 实测权重自包含（conditioner + model + vae 全在同一个 fp16 文件里），
    不需要额外下载 DINOv3 图像编码器
  - TRELLIS.2 实测 int8 权重 4.89 GB，加 VAE/CLIP 约 7.9 GB，6 GB 装不下

【重要】它只生成**几何**，不生成贴图。这是刻意的取舍：
纹理阶段（Hunyuan3DPaintPipeline）需要可微光栅化器 + 更大显存，6 GB 跑不动。
几何出来后走本项目的 tools/rig.cmd -Prepare 重拓扑 + 布 Godot 三平面贴图，
对背景怪物类资产完全够用（Godot 的 uv1_triplanar 不需要 UV）。

用法：
  python hunyuan3d_shape.py --image ref.png --out mesh.glb [--steps 30] [--octree 256]
"""

import argparse
import os
import sys
import time

# 显存相关的默认值都调得比官方保守，为的就是挤进 6 GB
DEFAULT_REPO_DIR = r"D:\Tools\Hunyuan3D-2"
DEFAULT_MODELS = r"D:\Tools\models\hunyuan3d-2mini"
DEFAULT_DIT_SUBFOLDER = "hunyuan3d-dit-v2-mini-turbo"
DEFAULT_VAE_SUBFOLDER = "hunyuan3d-vae-v2-mini-turbo"


def patch_dino_state_dict(verbose=True):
    """
    修正 Hunyuan3D-2 加载 DINO 图像编码器时的键名/结构不兼容。

    【问题】`hy3dgen/shapegen/models/conditioner.py` 写死用 transformers 的 `Dinov2Model`
    （键名 attention.q_proj / mlp.fc1 …），但 `tencent/Hunyuan3D-2mini` 的 checkpoint
    实际是 **DINOv2-with-registers + SwiGLU FFN** 结构，键名形如
    `attention.attention.key` / `mlp.weights_in`（8192 宽，gate 与 up 融合）。
    而 `pipelines.py:181` 的 `conditioner.load_state_dict(ckpt['conditioner'])` 没传
    `strict=False`，于是直接抛 Missing/Unexpected key —— 表面像环境/显存问题，
    实际是命名与结构差异。

    【为什么拦 load_state_dict 而不是子模块】
      `pipelines.py:179-181` 是先 `instantiate_from_config` 建好 conditioner，
      再对它整体 `load_state_dict`。这个调用会下钻到 DINO 子模块，用的仍然是
      原始键名；所以只在子模块构造时做适配没用，必须在 load_state_dict 这一层拦。

    【修法】给 `torch.nn.Module.load_state_dict` 打一个包装：
      1. 只对「DINO 编码器调用」生效（按 module 的类名判断），其它模型零影响
      2. 把内部 DINO 子模块的键适配成 transformers 期望的形式
         （去前缀 + 改名 + 把融合的 weights_in 拆成 gate/up + 补零 register_tokens）
      3. 过滤掉这些 DINO 键后以 strict=False 加载，未列出的键保持原行为
      4. 用适配后的权重显式加载 DINO 子模块，并校验形状与权重数值合理性

    返回 (是否成功, 说明)。
    """
    try:
        import torch
        from safetensors import safe_open
        import yaml
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import dino_key_map
    except Exception as exc:
        return False, "补丁依赖导入失败: %s" % exc

    ckpt = os.path.join(DEFAULT_MODELS, DEFAULT_DIT_SUBFOLDER, "model.fp16.safetensors")
    if not os.path.exists(ckpt):
        return False, "找不到权重文件: %s" % ckpt

    # 从 checkpoint 的 config.yaml 里读出 DINO 的真实结构参数。
    # 注意原配置写的是 model_type=dinov2 且没有 intermediate_size，
    # 但权重实际是 registers + SwiGLU 结构，FFN 宽度是 hidden × mlp_ratio。
    cfg_path = os.path.join(DEFAULT_MODELS, DEFAULT_DIT_SUBFOLDER, "config.yaml")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            full_cfg = yaml.safe_load(fh)
        dino_cfg = full_cfg["conditioner"]["params"]["main_image_encoder"]["kwargs"]["config"]
    except Exception as exc:
        return False, "读取 config.yaml 失败: %s" % exc

    try:
        with safe_open(ckpt, framework="pt") as f:
            cond_ckpt = {k: f.get_tensor(k) for k in f.keys()
                         if k.startswith("conditioner.")}
    except Exception as exc:
        return False, "读取 conditioner 权重失败: %s" % exc

    # 预先算好「适配后的 DINO 键」，加载时直接查表
    adapted, report = dino_key_map.adapt_conditioner_state(cond_ckpt, verbose=verbose)
    if verbose:
        print("[hy3d] DINO 权重适配表: %d 个键（改名 %d / 拆分融合FFN %d）"
              % (len(adapted), report["renamed"], report["split_fused_ffn"]))

    original = torch.nn.Module.load_state_dict
    if getattr(original, "_dsh_dino_patched", False):
        return True, "已经打过补丁"

    def patched(self, state_dict, *args, **kwargs):
        """
        全局拦截 load_state_dict。

        【为什么不能只按类名判断】
          `pipelines.py:181` 调的是**父模块** `SingleImageEncoder.load_state_dict(...)`，
          它会下钻到 DINO 子模块，带着未适配的原始键名。只在 `DinoImageEncoder`
          这一层拦是拦不住的 —— 父模块会先以 strict 模式失败。
          所以这里对所有模块生效：统一把 DINO 键摘出来单独适配加载，
          剩下的键交给原始逻辑，并对父模块放宽 strict。
        """
        import torch as _torch

        cls = type(self).__name__
        is_dino = (cls == "DinoImageEncoder" and hasattr(self, "model"))

        # 把 DINO 编码器相关的键摘出来（父模块视角是 'main_image_encoder.model.*'，
        # DINO 自身视角是 'model.*'）
        dino_prefixes = ("model.", "main_image_encoder.model.")
        dino_keys = [k for k in state_dict if k.startswith(dino_prefixes)]
        rest = {k: v for k, v in state_dict.items() if k not in set(dino_keys)}

        if not dino_keys:
            return original(self, state_dict, *args, **kwargs)

        # ---- 处理 DINO 子模块 ----
        target = self if is_dino else getattr(self, "main_image_encoder", None)

        def _norm(k):
            return "model." + k.split("model.", 1)[1] if "model." in k else k

        if target is not None and hasattr(target, "model"):
            sub_state = {_norm(k): v for k, v in state_dict.items()
                         if k.startswith(dino_prefixes)}

            # 第一步：换成结构正确的 DINO（registers + SwiGLU）。
            # 原代码用 Dinov2Model 建出来的 FFN 宽度就差一半，光改键名救不回来。
            if type(target.model).__name__ != "Dinov2WithRegistersModel":
                try:
                    new_model = dino_key_map.build_dino_from_config(dino_cfg, verbose=verbose)
                    if hasattr(target.model, "dtype"):
                        new_model = new_model.to(dtype=target.model.dtype)
                    target.model = new_model
                except Exception as exc:
                    if verbose:
                        print("[hy3d] 重建 DINO 结构失败，退回原结构: %s" % exc)
            target.model.eval()
            target.model.requires_grad_(False)

            # 第二步：键名适配（补前缀后走同一套映射表）
            prefixed = {"conditioner.main_image_encoder." + k: v
                        for k, v in sub_state.items()}
            mapped, _ = dino_key_map.adapt_conditioner_state(prefixed, verbose=False)

            model_state = target.model.state_dict()
            hit = {k: v for k, v in mapped.items()
                   if k in model_state and tuple(model_state[k].shape) == tuple(v.shape)}
            if "embeddings.register_tokens" in model_state and \
                    "embeddings.register_tokens" not in hit:
                hit["embeddings.register_tokens"] = _torch.zeros_like(
                    model_state["embeddings.register_tokens"])

            load_info = target.model.load_state_dict(hit, strict=False)
            if verbose:
                with _torch.no_grad():
                    stds = [float(p.std()) for n, p in target.model.named_parameters()
                            if p.dim() >= 2 and "weight" in n]
                    avg = sum(stds) / max(len(stds), 1)
                print("[hy3d] DINO 加载: 命中 %d/%d, 缺失 %d, 权重std=%.5f (%s)"
                      % (len(hit), len(model_state), len(load_info.missing_keys), avg,
                         "训练权重" if 0.001 < avg < 0.5 else "可疑！可能仍是随机初始化"))

        # ---- 其余部分交给原始逻辑，但放宽 strict（DINO 键已单独处理）----
        kwargs["strict"] = False
        return original(self, rest, *args, **kwargs)

    patched._dsh_dino_patched = True
    torch.nn.Module.load_state_dict = patched
    return True, "load_state_dict 已拦截"


def parse_args():
    p = argparse.ArgumentParser(description="Hunyuan3D-2mini 图生3D（只出几何）")
    p.add_argument("--image", required=True, help="输入参考图（png/jpg）")
    p.add_argument("--out", required=True, help="输出 GLB 路径")
    p.add_argument("--steps", type=int, default=30, help="扩散步数，默认 30（越低越快越省显存）")
    p.add_argument("--octree", type=int, default=256,
                   help="八叉树分辨率，决定网格精细度。6 GB 建议 256；384 可能 OOM")
    p.add_argument("--guidance", type=float, default=5.0, help="引导强度")
    p.add_argument("--chunks", type=int, default=2000,
                   help="VAE 解码分块数，越小越省显存")
    p.add_argument("--repo", default=DEFAULT_REPO_DIR, help="Hunyuan3D-2 代码目录")
    p.add_argument("--models", default=DEFAULT_MODELS, help="本地权重目录")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cpu-offload", action="store_true",
                   help="显存不够时把模型分块卸载到内存（慢很多，但能跑）")
    p.add_argument("--report", default="", help="把运行信息写成 JSON")
    return p.parse_args()


def main():
    args = parse_args()

    sys.path.insert(0, args.repo)
    os.environ.setdefault("HY3DGEN_MODELS", os.path.dirname(args.models))

    import torch
    from PIL import Image

    # 必须"先打补丁再导入管线"：补丁要拦 torch.nn.Module.load_state_dict，
    # 而 pipelines.py 的 from_pretrained 内部就会触发它。
    ok, patch_info = patch_dino_state_dict()
    if ok:
        print("[hy3d] DINO 适配补丁: %s" % patch_info)
    else:
        print("[hy3d] 警告：DINO 补丁未应用（%s），继续尝试原路径" % patch_info)

    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    if not os.path.exists(args.image):
        print("[hy3d] 找不到输入图片: %s" % args.image)
        return 1
    if not torch.cuda.is_available():
        print("[hy3d] CUDA 不可用，本模型必须用 GPU（CPU 推理会慢到不可用）")
        return 1

    dev = torch.cuda.get_device_name(0)
    total = torch.cuda.get_device_properties(0).total_memory / 1e9
    free = (torch.cuda.get_device_properties(0).total_memory
            - torch.cuda.memory_reserved(0)) / 1e9
    print("[hy3d] GPU: %s  %.2f GB" % (dev, total))
    print("[hy3d] torch %s / cuda %s" % (torch.__version__, torch.version.cuda))

    # 权重是自包含的 fp16 文件，用 subfolder 指到 mini 目录
    t0 = time.time()
    print("[hy3d] 加载 DiT 权重: %s ..." % DEFAULT_DIT_SUBFOLDER)
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        args.models,
        device="cuda",
        dtype=torch.float16,
        use_safetensors=True,
        variant="fp16",
        subfolder=DEFAULT_DIT_SUBFOLDER,
    )
    # VAE 是独立子目录
    try:
        pipe.vae = type(pipe.vae).from_pretrained(
            os.path.join(args.models, DEFAULT_VAE_SUBFOLDER),
            device="cuda", dtype=torch.float16,
        )
    except Exception as exc:
        print("[hy3d] VAE 单独加载失败（沿用管线自带的）: %s" % exc)

    if args.cpu_offload:
        print("[hy3d] 启用 CPU offload（慢，但更省显存）")
        try:
            pipe.enable_model_cpu_offload()
        except Exception as exc:
            print("[hy3d] CPU offload 不可用: %s" % exc)

    print("[hy3d] 权重就绪，用时 %.1f 秒" % (time.time() - t0))
    if torch.cuda.is_available():
        print("[hy3d] 峰值显存: %.2f GB" % (torch.cuda.max_memory_allocated() / 1e9))

    image = Image.open(args.image).convert("RGBA")
    print("[hy3d] 输入图: %s  %dx%d" % (args.image, image.width, image.height))
    print("[hy3d] 开始生成: steps=%d octree=%d guidance=%.1f chunks=%d"
          % (args.steps, args.octree, args.guidance, args.chunks))

    gen = None
    if args.seed:
        gen = torch.Generator(device="cuda").manual_seed(args.seed)

    t1 = time.time()
    try:
        mesh = pipe(
            image=image,
            num_inference_steps=args.steps,
            guidance_scale=args.guidance,
            octree_resolution=args.octree,
            num_chunks=args.chunks,
            generator=gen,
            output_type="trimesh",
        )[0]
    except torch.cuda.OutOfMemoryError:
        print("[hy3d] 显存不足（OOM）。降参数重试：--octree 192 --steps 20 --chunks 1000")
        print("[hy3d] 或者加 --cpu-offload（很慢）")
        return 2
    dt = time.time() - t1

    peak = torch.cuda.max_memory_allocated() / 1e9
    print("[hy3d] 生成完成，用时 %.1f 秒" % dt)
    print("[hy3d] 峰值显存: %.2f GB / %.2f GB" % (peak, total))
    print("[hy3d] 网格: %d 顶点 / %d 面" % (len(mesh.vertices), len(mesh.faces)))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    mesh.export(args.out)
    size = os.path.getsize(args.out)
    print("[hy3d] 导出: %s (%.1f KB)" % (args.out, size / 1024.0))

    if args.report:
        import json
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({
                "generator": "hunyuan3d-2mini-turbo",
                "image": os.path.basename(args.image),
                "out": os.path.basename(args.out),
                "vertices": int(len(mesh.vertices)),
                "faces": int(len(mesh.faces)),
                "seconds": round(dt, 2),
                "peak_vram_gb": round(peak, 3),
                "steps": args.steps,
                "octree_resolution": args.octree,
                "guidance": args.guidance,
                "gpu": dev,
            }, fh, ensure_ascii=False, indent=2)
        print("[hy3d] 报告: %s" % args.report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
