"""
剥离 GLB 里的骨骼/蒙皮/动画，只保留网格与材质。

用途有两个：
  1) **造测试样本**：图生3D / 文生3D 的产出就是「只有网格、没有骨骼」。
     拿现成的绑定模型剥掉骨骼，就得到和真实生成结果等价的输入，
     用来验证 auto_rig.py 的自动绑定是否真的可用。
  2) **清理外部资产**：从网上/商店拿到的模型常常带着一堆没用的骨骼和动画，
     进 Godot 之前先剥干净，能显著减小体积、避免导入出一堆垃圾 AnimationPlayer。

只用标准库实现（不依赖 Blender / numpy / pygltflib），所以能离线快速跑。

用法：
    python strip_rig.py --in 输入.glb --out 输出.glb [--keep-materials] [--report 报告.json]
"""

import argparse
import json
import os
import struct
import sys

# glTF 组件类型 -> (struct 格式, 每元素字节数)
COMPONENT_TYPES = {
    5120: ("b", 1),   # BYTE
    5121: ("B", 1),   # UNSIGNED_BYTE
    5122: ("h", 2),   # SHORT
    5123: ("H", 2),   # UNSIGNED_SHORT
    5125: ("I", 4),   # UNSIGNED_INT
    5126: ("f", 4),   # FLOAT
}

TYPE_COMPONENTS = {
    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942

#: 蒙皮相关的顶点属性，剥离时要去掉
SKIN_ATTRIBUTES = ("JOINTS_0", "JOINTS_1", "WEIGHTS_0", "WEIGHTS_1")


def read_glb(path):
    """读 GLB，返回 (json_dict, bin_bytes)。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 12 or data[:4] != b"glTF":
        raise ValueError("不是合法的 GLB 文件（缺少 glTF magic）: %s" % path)
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise ValueError("只支持 glTF 2.0，当前版本号 %d" % version)

    gltf, binary = None, b""
    off = 12
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        payload = data[off:off + clen]
        off += clen
        if ctype == JSON_CHUNK:
            gltf = json.loads(payload.decode("utf-8"))
        elif ctype == BIN_CHUNK:
            binary = payload
    if gltf is None:
        raise ValueError("GLB 里没有 JSON chunk")
    return gltf, binary


def write_glb(path, gltf, binary):
    """把 json + bin 写回 GLB。"""
    json_bytes = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)      # 4 字节对齐
    bin_bytes = binary + b"\x00" * ((4 - len(binary) % 4) % 4)

    chunks = struct.pack("<II", len(json_bytes), JSON_CHUNK) + json_bytes
    if bin_bytes:
        chunks += struct.pack("<II", len(bin_bytes), BIN_CHUNK) + bin_bytes
    total = 12 + len(chunks)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"glTF" + struct.pack("<II", 2, total) + chunks)


def accessor_element_size(gltf, acc_index):
    acc = gltf["accessors"][acc_index]
    fmt, size = COMPONENT_TYPES[acc["componentType"]]
    ncomp = TYPE_COMPONENTS[acc["type"]]
    return size * ncomp, fmt, ncomp


def read_accessor_raw(gltf, binary, acc_index):
    """
    读出一个 accessor 的原始字节。

    bufferView 可能带 byteStride（交错存储），这时要按 stride 逐元素抽出来。
    """
    acc = gltf["accessors"][acc_index]
    if "bufferView" not in acc:
        # 稀疏或纯零数据，按元素大小返回全零
        elem, _, _ = accessor_element_size(gltf, acc_index)
        return b"\x00" * (elem * acc["count"])

    bv = gltf["bufferViews"][acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    elem, _, _ = accessor_element_size(gltf, acc_index)
    stride = bv.get("byteStride", 0)

    if not stride or stride == elem:
        return binary[base:base + elem * acc["count"]]

    out = bytearray()
    for i in range(acc["count"]):
        start = base + i * stride
        out += binary[start:start + elem]
    return bytes(out)


def strip(in_path, out_path, keep_materials=True, report_path=""):
    gltf, binary = read_glb(in_path)

    removed = {
        "skins": len(gltf.get("skins", [])),
        "animations": len(gltf.get("animations", [])),
        "skin_attributes": 0,
        "joint_nodes": 0,
        "materials": 0,
        "textures": 0,
    }

    # ---- 1) 去掉 skin 引用与 skins ----
    for node in gltf.get("nodes", []):
        node.pop("skin", None)
    gltf.pop("skins", None)
    gltf.pop("animations", None)

    # ---- 2) 去掉网格上的蒙皮属性，并重建 bufferView/accessor ----
    old_views = gltf.get("bufferViews", [])
    old_accessors = gltf.get("accessors", [])

    # 收集所有还需要保留的 accessor（网格属性、索引、以及被复用的其它 accessor）
    used_accessors = set()
    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            attrs = prim.get("attributes", {})
            for name in list(attrs.keys()):
                if name in SKIN_ATTRIBUTES:
                    del attrs[name]
                    removed["skin_attributes"] += 1
            for acc in attrs.values():
                used_accessors.add(acc)
            if "indices" in prim:
                used_accessors.add(prim["indices"])
            for target in prim.get("targets", []) or []:
                for acc in target.values():
                    used_accessors.add(acc)

    # 把 accessor 重新编号并打包进新 buffer
    new_bin = bytearray()
    acc_map = {}
    new_accessors = []
    new_views = []

    for old_idx in sorted(used_accessors):
        acc = dict(old_accessors[old_idx])
        raw = read_accessor_raw(gltf, binary, old_idx)

        # 新 bufferView：紧密排列，不带 stride
        while len(new_bin) % 4:
            new_bin.append(0)
        offset = len(new_bin)
        new_bin += raw
        view_index = len(new_views)
        new_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(raw),
            "target": 34962 if acc["type"].startswith("VEC") else 34963,
        })

        acc.pop("bufferView", None)
        acc.pop("byteOffset", None)
        acc.pop("sparse", None)
        acc["bufferView"] = view_index
        acc_map[old_idx] = len(new_accessors)
        new_accessors.append(acc)

    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if "attributes" in prim:
                prim["attributes"] = {k: acc_map[v] for k, v in prim["attributes"].items()}
            if "indices" in prim and prim["indices"] in acc_map:
                prim["indices"] = acc_map[prim["indices"]]

    gltf["accessors"] = new_accessors
    gltf["bufferViews"] = new_views
    gltf["buffers"] = [{"byteLength": len(new_bin)}]

    # ---- 3) 可选的材质/贴图清理 ----
    if not keep_materials:
        removed["materials"] = len(gltf.get("materials", []))
        removed["textures"] = len(gltf.get("textures", []))
        for mesh in gltf.get("meshes", []):
            for prim in mesh.get("primitives", []):
                prim.pop("material", None)
        for key in ("materials", "textures", "images", "samplers"):
            gltf.pop(key, None)

    # ---- 4) 统计剩余的关节节点（已变成无用的空节点，保留是为了不破坏层级）----
    if gltf.get("nodes"):
        removed["joint_nodes"] = sum(
            1 for n in gltf["nodes"] if "mesh" not in n and "children" not in n
            and "translation" in n or ("mesh" not in n and "name" in n and "children" not in n)
        )

    write_glb(out_path, gltf, bytes(new_bin))

    info = {
        "input": os.path.basename(in_path),
        "output": os.path.basename(out_path),
        "input_bytes": os.path.getsize(in_path),
        "output_bytes": os.path.getsize(out_path),
        "removed": removed,
        "meshes": len(gltf.get("meshes", [])),
        "nodes": len(gltf.get("nodes", [])),
    }
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=2)
    return info


def main():
    p = argparse.ArgumentParser(description="剥离 GLB 的骨骼/蒙皮/动画，只留网格")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--out", dest="dst", required=True)
    p.add_argument("--drop-materials", action="store_true", help="连材质一起剥掉")
    p.add_argument("--report", default="")
    args = p.parse_args()

    info = strip(args.src, args.dst, keep_materials=not args.drop_materials,
                 report_path=args.report)
    print("[strip_rig] %s -> %s" % (info["input"], info["output"]))
    print("[strip_rig] 体积 %.1f KB -> %.1f KB" % (info["input_bytes"] / 1024.0,
                                                   info["output_bytes"] / 1024.0))
    print("[strip_rig] 移除: 骨架 %d 个 / 动画 %d 段 / 蒙皮属性 %d 项"
          % (info["removed"]["skins"], info["removed"]["animations"],
             info["removed"]["skin_attributes"]))
    print("[strip_rig] 剩余: 网格 %d / 节点 %d" % (info["meshes"], info["nodes"]))


if __name__ == "__main__":
    sys.exit(main())
