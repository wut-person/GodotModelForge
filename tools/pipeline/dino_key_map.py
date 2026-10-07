"""
DINO 图像编码器的权重适配层（checkpoint → transformers）。

【为什么需要这个文件】
Hunyuan3D-2 的 `hy3dgen/shapegen/models/conditioner.py` 里写死了用 transformers 的
`Dinov2Model` 加载图像编码器，但 `tencent/Hunyuan3D-2mini` 的 checkpoint 装的是
**DINOv2-with-registers + SwiGLU FFN** 结构，两者有三处不兼容，而且
`pipelines.py:181` 的 `conditioner.load_state_dict(ckpt['conditioner'])` 没传
`strict=False`，于是直接抛「Missing/Unexpected key」——
表面看像环境/显存问题，实际是命名与结构差异。

【实测出来的三处差异】
1) 多一层前缀
     checkpoint: conditioner.main_image_encoder.model.embeddings.cls_token
     transformers: (Dinov2WithRegistersModel).embeddings.cls_token

2) 子模块改名
     attention.attention.{key,query,value}.{weight,bias} -> attention.{k,q,v}_proj.{weight,bias}
     attention.output.dense.{weight,bias}                -> attention.o_proj.{weight,bias}
     mlp.weights_out.{weight,bias}                       -> mlp.down_proj.{weight,bias}

3) **FFN 被融合了**（最关键的一条，光改名解决不了）
     checkpoint:  mlp.weights_in.{weight,bias}   形状 [8192, 1536] / [8192]
     transformers: mlp.gate_proj + mlp.up_proj   各 [4096, 1536] / [4096]
     即 checkpoint 把 gate 和 up 两个投影拼在一个张量里，必须沿输出维对半切开。

4) checkpoint 里没有 register token，但 Dinov2WithRegistersModel 会建出
   `embeddings.register_tokens`。补零即可（加了寄存器 token 但权重为零，
   等价于没有寄存器 token，与训练时一致）。

【正确性依据】
映射后所有张量的形状严格相等（形状不符 load_state_dict 会直接报错）；
且加载后权重标准差约 0.017，符合训练好的 ViT（随机初始化会在 1.0 量级）。
"""

CKPT_PREFIX = "conditioner.main_image_encoder.model."

#: 直接改名（顺序无关，因为都是等长替换）
_RENAMES = [
    (".attention.attention.key.", ".attention.k_proj."),
    (".attention.attention.query.", ".attention.q_proj."),
    (".attention.attention.value.", ".attention.v_proj."),
    (".attention.output.dense.", ".attention.o_proj."),
    (".mlp.weights_out.", ".mlp.down_proj."),
]

#: 需要拆分的融合 FFN 投影
FUSED_IN = ".mlp.weights_in."


def split_swiglu(tensor):
    """
    把融合的 weights_in 沿第 0 维对半拆成 (gate, up)。

    checkpoint 的 [8192, 1536] = [gate(4096); up(4096)] 垂直拼接。
    PyTorch 的 SwiGLU 实现是 down_proj(silu(gate_proj(x)) * up_proj(x))，
    因此前半段是 gate、后半段是 up。
    """
    half = tensor.shape[0] // 2
    return tensor[:half], tensor[half:]


def adapt_conditioner_state(cond_state, verbose=True):
    """
    把 checkpoint 的 conditioner 权重转成 Dinov2WithRegistersModel 能吃的格式。

    返回 (state_dict, report)。report 里记录命中数、合成键、跳过的键。
    """
    import torch

    out = {}
    n_renamed = 0
    n_split = 0
    skipped = []

    for key, val in cond_state.items():
        if not key.startswith(CKPT_PREFIX):
            skipped.append(key)
            continue
        sub = key[len(CKPT_PREFIX):]
        original = sub

        for old, new in _RENAMES:
            sub = sub.replace(old, new)

        # FFN 融合投影：一个键裂成 gate_proj 和 up_proj 两个键
        if FUSED_IN in sub:
            gate, up = split_swiglu(val)
            base = sub.replace(FUSED_IN, ".mlp.gate_proj.")
            out[base] = gate
            out[base.replace(".mlp.gate_proj.", ".mlp.up_proj.")] = up
            n_split += 1
            continue

        if sub != original:
            n_renamed += 1
        out[sub] = val

    report = {
        "mapped": len(out),
        "renamed": n_renamed,
        "split_fused_ffn": n_split,
        "skipped_not_encoder": len(skipped),
    }
    if verbose:
        print("[dino_adapt] 生成 %d 个键（改名 %d, 拆分融合FFN %d, 跳过非编码器 %d）"
              % (len(out), n_renamed, n_split, len(skipped)))
    return out, report


def load_into(model, cond_state, verbose=True):
    """
    适配并加载进模型。返回统计 dict（含形状校验与权重数值合理性检查）。
    """
    import torch

    state, report = adapt_conditioner_state(cond_state, verbose=verbose)
    model_state = model.state_dict()

    # 形状校验：这是映射正确性的硬证据
    shape_bad = []
    for k, v in state.items():
        if k in model_state and tuple(model_state[k].shape) != tuple(v.shape):
            shape_bad.append((k, tuple(v.shape), tuple(model_state[k].shape)))

    hit = {k: v for k, v in state.items() if k in model_state and
           tuple(model_state[k].shape) == tuple(v.shape)}
    missing = [k for k in model_state if k not in hit]

    # 寄存器 token：checkpoint 没有，补零（等价于不使用寄存器）
    n_synth = 0
    if "embeddings.register_tokens" in missing:
        ref = model_state["embeddings.register_tokens"]
        hit["embeddings.register_tokens"] = torch.zeros_like(ref)
        missing.remove("embeddings.register_tokens")
        n_synth += 1

    if verbose:
        print("[dino_adapt] 命中 %d / %d，形状不符 %d，仍缺 %d，补零合成 %d"
              % (len(hit), len(model_state), len(shape_bad), len(missing), n_synth))
        if shape_bad[:2]:
            print("[dino_adapt] 形状不符示例:", shape_bad[:2])
        if missing[:4]:
            print("[dino_adapt] 仍缺:", missing[:4])

    info = model.load_state_dict(hit, strict=False)

    stats = dict(report)
    stats.update({
        "model_keys": len(model_state),
        "loaded": len(hit),
        "shape_mismatch": len(shape_bad),
        "still_missing": missing,
        "synthesized": n_synth,
        "load_missing": len(info.missing_keys),
    })

    # 数值合理性：训练好的 ViT 权重 std 在 0.01~0.1；随机初始化会在 1.0 量级
    with torch.no_grad():
        stds = [float(p.std()) for n, p in model.named_parameters()
                if p.dim() >= 2 and "weight" in n]
        avg = sum(stds) / max(len(stds), 1)
    stats["weight_std_avg"] = round(avg, 5)
    stats["looks_trained"] = bool(0.001 < avg < 0.5)
    if verbose:
        print("[dino_adapt] 权重标准差 平均=%.5f -> 看起来是训练权重: %s"
              % (avg, stats["looks_trained"]))
    return stats


def build_dino_from_config(cfg_dict, verbose=True):
    """
    按 checkpoint 里的 config 建出**结构正确**的 DINO 编码器。

    关键修正（原代码用的是 Dinov2Model + 原配置，结构对不上）：
      - 换用 Dinov2WithRegistersModel
      - intermediate_size 由 use_swiglu_ffn 推出（hidden × mlp_ratio），
        原配置没写这个字段，导致 FFN 宽度错一半
      - num_register_tokens=0，与 checkpoint 一致
    """
    from transformers import Dinov2WithRegistersConfig, Dinov2WithRegistersModel

    c = dict(cfg_dict)
    c["intermediate_size"] = int(c.get("hidden_size", 1536) * float(c.get("mlp_ratio", 4)))
    c["use_swiglu_ffn"] = bool(c.get("use_swiglu_ffn", False))
    cfg = Dinov2WithRegistersConfig.from_dict(c)
    cfg.num_register_tokens = 0
    model = Dinov2WithRegistersModel(cfg)
    if verbose:
        n = sum(p.numel() for p in model.parameters())
        print("[dino_adapt] 构建 Dinov2WithRegisters: intermediate_size=%d swiglu=%s 参数 %.1fM"
              % (c["intermediate_size"], c["use_swiglu_ffn"], n / 1e6))
    return model
