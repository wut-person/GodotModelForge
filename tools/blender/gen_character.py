"""
Blender 无头建模管线：程序化生成「带骨骼 + 蒙皮 + 动画片段」的 3D 角色，导出 GLB 给 Godot。

这个文件有三个身份，后期逐步增强：
  1) 基线自检模型 —— 证明「Blender 生成 → GLB → Godot 导入 → 无头验证」整条链路是通的；
  2) 比例/骨架的参考实现 —— 定义人形角色的骨骼命名与位置规范，AI 生成的网格可以套用同一套骨架；
  3) 后期 AI 管线的收尾工序 —— 图生3D 出来的网格是三角汤、没骨骼、没 UV，
     最终仍要经过本脚本的「套骨架 → 算权重 → 烘焙动画 → 导出 GLB」这一步。

用法（由 tools/pipeline/build_character.ps1 调用）：
  blender --background --factory-startup --python gen_character.py -- \
      --name hero --out "D:/.../assets/characters/hero" --seed 20261008

注意：`--` 之后的参数才是本脚本的参数，Blender 自己的参数必须在 `--` 之前。
"""

import argparse
import json
import math
import os
import sys
import warnings

import bpy
import mathutils
from mathutils import Vector, Euler

# 同目录的共享库（骨骼规范 / 蒙皮权重 / 动画库 / 导出）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rig_lib

# Blender 5.x 对部分旧属性发 DeprecationWarning，这些警告会走 stderr，
# 在 PowerShell 的 $ErrorActionPreference='Stop' 下会被当成致命错误，直接静音。
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Windows 控制台默认是 GBK，中文日志会变乱码；强制 UTF-8 便于 DSH 读取日志
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 两种运行方式：
#   A) Blender 可执行文件：blender --background --python gen_character.py -- <参数>
#   B) bpy Python 模块：  python -m ... 直接 import 本文件后调用 main()
# 两者在「能否重置场景」上有差别，用这个标志区分（脚本刚启动时 bpy 已在 sys.modules 里 => 模块方式）。
RUNNING_AS_MODULE = "bpy" in sys.modules

# --------------------------------------------------------------------------- 参数


def parse_args():
    argv = sys.argv
    # Blender 可执行文件用 `--` 分隔自己的参数与脚本参数
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    elif RUNNING_AS_MODULE:
        # 模块方式由启动器传入：python runner.py <参数...>，第一个元素是启动器路径
        argv = argv[1:]
    else:
        argv = []
    p = argparse.ArgumentParser(description="程序化生成带骨骼动画的 Godot 角色 GLB")
    p.add_argument("--name", default="hero", help="资产名（决定输出文件名）")
    p.add_argument("--out", required=True, help="输出目录（绝对路径）")
    p.add_argument("--seed", type=int, default=0, help="随机种子（用于体型微调）")
    p.add_argument("--height", type=float, default=1.8, help="角色总高（米）")
    p.add_argument("--no-anim", action="store_true", help="不生成动画（只出静态绑定模型）")
    p.add_argument("--no-export", action="store_true", help="只建模不导出（调试用）")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- 工具


def purge_scene():
    """
    清空场景，保证结果可复现。

    注意：`wm.read_factory_settings` 在 bpy 模块模式下会重建整个上下文，容易把宿主进程搞崩，
    所以模块模式下改为「手动删对象 + 删数据块」，效果等价。
    """
    if not RUNNING_AS_MODULE:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    else:
        for obj in list(bpy.data.objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
    for block in (bpy.data.meshes, bpy.data.objects, bpy.data.armatures,
                  bpy.data.materials, bpy.data.actions, bpy.data.images):
        for item in list(block):
            try:
                block.remove(item)
            except Exception:
                pass


def build_skeleton(height=1.8):
    """
    人形骨架定义，直接复用共享库 rig_lib 里的规范（与 auto_rig.py 同一套骨骼名）。
    """
    return rig_lib.skeleton(scaled=True, height=height)


def build_body_boxes(height=1.8):
    """
    用「锥台段」拼出身体各部位，并标注该段应当跟随哪根骨骼。
    每段用底部/顶部两个矩形 + 中心偏移描述，比纯立方体更容易做出人形轮廓。
    返回 [(vert_positions, faces, bone_name, material_index), ...]
    """
    s = height / 1.8
    parts = []

    def seg(name, bone_name, mat, z0, z1, half_x0, half_y0, half_x1, half_y1,
            x=0.0, y=0.0, x1=None, y1=None):
        """一个从 z0 到 z1 的锥台段。"""
        x1 = x if x1 is None else x1
        y1 = y if y1 is None else y1
        z0 *= s; z1 *= s
        hx0, hy0, hx1, hy1 = half_x0 * s, half_y0 * s, half_x1 * s, half_y1 * s
        cx0, cy0 = x * s, y * s
        cx1, cy1 = x1 * s, y1 * s
        verts = [
            (cx0 - hx0, cy0 - hy0, z0), (cx0 + hx0, cy0 - hy0, z0),
            (cx0 + hx0, cy0 + hy0, z0), (cx0 - hx0, cy0 + hy0, z0),
            (cx1 - hx1, cy1 - hy1, z1), (cx1 + hx1, cy1 - hy1, z1),
            (cx1 + hx1, cy1 + hy1, z1), (cx1 - hx1, cy1 + hy1, z1),
        ]
        faces = [
            (0, 1, 2, 3),          # 底
            (7, 6, 5, 4),          # 顶
            (0, 4, 5, 1),          # -Y
            (1, 5, 6, 2),          # +X
            (2, 6, 7, 3),          # +Y
            (3, 7, 4, 0),          # -X
        ]
        parts.append({"name": name, "verts": verts, "faces": faces,
                      "bone": bone_name, "mat": mat})

    # 骨盆 / 腰 / 胸 / 颈 / 头
    seg("pelvis", "hips",  0, 0.94, 1.13, 0.13, 0.095, 0.125, 0.09)
    seg("waist",  "spine", 0, 1.13, 1.30, 0.11, 0.085, 0.135, 0.095)
    seg("chest",  "chest", 0, 1.30, 1.50, 0.15, 0.10, 0.115, 0.085)
    seg("neck",   "neck",  0, 1.50, 1.585, 0.05, 0.05, 0.055, 0.055)
    seg("head",   "head",  0, 1.585, 1.80, 0.075, 0.085, 0.055, 0.06,
        y=-0.01, y1=-0.015)

    # 手臂（上/前/手），左右对称
    for side, sx in (("L", 1.0), ("R", -1.0)):
        seg("upperarm." + side, "upperarm." + side, 1,
            1.43, 1.14, 0.055, 0.055, 0.045, 0.048, x=0.175 * sx)
        seg("forearm." + side, "forearm." + side, 1,
            1.14, 0.90, 0.045, 0.048, 0.038, 0.042, x=0.175 * sx)
        seg("hand." + side, "hand." + side, 1,
            0.90, 0.76, 0.045, 0.05, 0.035, 0.04, x=0.175 * sx)

    # 腿（大腿/小腿/脚）
    for side, sx in (("L", 1.0), ("R", -1.0)):
        seg("thigh." + side, "thigh." + side, 1,
            0.98, 0.52, 0.085, 0.09, 0.07, 0.075, x=0.095 * sx, x1=0.10 * sx)
        seg("shin." + side, "shin." + side, 1,
            0.52, 0.08, 0.07, 0.075, 0.055, 0.06, x=0.10 * sx, x1=0.105 * sx)
        # 脚朝 -Y（Blender 里角色正面方向）
        parts.append({
            "name": "foot." + side,
            "verts": [
                (0.105 * sx - 0.055, -0.19, 0.0), (0.105 * sx + 0.055, -0.19, 0.0),
                (0.105 * sx + 0.055, 0.075, 0.0), (0.105 * sx - 0.055, 0.075, 0.0),
                (0.105 * sx - 0.05, -0.175, 0.085 * (height / 1.8)), (0.105 * sx + 0.05, -0.175, 0.085 * (height / 1.8)),
                (0.105 * sx + 0.05, 0.06, 0.085 * (height / 1.8)), (0.105 * sx - 0.05, 0.06, 0.085 * (height / 1.8)),
            ],
            "faces": [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5),
                      (2, 3, 7, 6), (3, 0, 4, 7)],
            "bone": "foot." + side, "mat": 2,
        })

    return parts


# --------------------------------------------------------------------------- 权重
# 蒙皮权重算法统一放在 rig_lib.compute_weights()，这里只做一层薄封装，
# 保证「程序化模型」和「AI 生成的任意网格」用的是同一套绑定算法。


def compute_weights(verts, bone_list, max_influences=3, falloff=1.6):
    return rig_lib.compute_weights(verts, bone_list,
                                   max_influences=max_influences, falloff=falloff)


# --------------------------------------------------------------------------- 建模

def create_mesh_object(parts, bone_list):
    """把各段合并成一个网格对象，同时建好材质槽与顶点组。"""
    all_verts, all_faces, face_mats = [], [], []
    # 每段记录它「主要归属」的骨骼，用于兜底（距离权重之外的语义约束）
    vert_hint = []

    for part in parts:
        base = len(all_verts)
        all_verts.extend(part["verts"])
        vert_hint.extend([part["bone"]] * len(part["verts"]))
        for f in part["faces"]:
            all_faces.append(tuple(base + i for i in f))
            face_mats.append(part["mat"])

    # 躯干上不允许出现「手臂骨骼影响胸口」这类串扰：限制候选骨骼范围
    weights = compute_weights(all_verts, bone_list)

    mesh = bpy.data.meshes.new("CharacterMesh")
    mesh.from_pydata(all_verts, [], all_faces)
    mesh.validate(verbose=False)
    mesh.update()

    obj = bpy.data.objects.new("Character", mesh)
    bpy.context.collection.objects.link(obj)

    # --- 材质 ---
    mat_body = bpy.data.materials.new("M_Body")
    mat_body.use_nodes = True
    bsdf = mat_body.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.30, 0.42, 0.62, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.55
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = 0.1
    mat_limb = bpy.data.materials.new("M_Limb")
    mat_limb.use_nodes = True
    bsdf2 = mat_limb.node_tree.nodes.get("Principled BSDF")
    if bsdf2:
        bsdf2.inputs["Base Color"].default_value = (0.22, 0.30, 0.44, 1.0)
        bsdf2.inputs["Roughness"].default_value = 0.6
    mat_acc = bpy.data.materials.new("M_Accent")
    mat_acc.use_nodes = True
    bsdf3 = mat_acc.node_tree.nodes.get("Principled BSDF")
    if bsdf3:
        bsdf3.inputs["Base Color"].default_value = (0.85, 0.55, 0.18, 1.0)
        bsdf3.inputs["Roughness"].default_value = 0.45

    for m in (mat_body, mat_limb, mat_acc):
        obj.data.materials.append(m)
    for poly, mi in zip(mesh.polygons, face_mats):
        poly.material_index = mi
        poly.use_smooth = False  # 低多边形硬边风格

    # --- 顶点组（蒙皮权重） ---
    groups = {}
    for b in bone_list:
        groups[b["name"]] = obj.vertex_groups.new(name=b["name"])
    for vi, w in enumerate(weights):
        for bname, val in w.items():
            if val > 1e-4:
                groups[bname].add([vi], val, "REPLACE")

    return obj


def create_armature(bone_list, name="Armature"):
    arm_data = bpy.data.armatures.new("CharacterArmature")
    arm_obj = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    # 操作符在模块模式下对上下文很敏感，用一个临时 override 保证一定能进编辑模式
    try:
        with bpy.context.temp_override(active_object=arm_obj, selected_objects=[arm_obj],
                                       object=arm_obj):
            bpy.ops.object.mode_set(mode="EDIT")
    except Exception:
        bpy.ops.object.mode_set(mode="EDIT")
    created = {}
    for b in bone_list:
        eb = arm_data.edit_bones.new(b["name"])
        eb.head = b["head"]
        eb.tail = b["tail"]
        eb.roll = 0.0
        created[b["name"]] = eb
    for b in bone_list:
        if b["parent"]:
            created[b["name"]].parent = created[b["parent"]]
            created[b["name"]].use_connect = bool(b["connect"])
    bpy.ops.object.mode_set(mode="OBJECT")

    for pb in arm_obj.pose.bones:
        pb.rotation_mode = "XYZ"
    return arm_obj


def bind(mesh_obj, arm_obj):
    """用 Armature 修改器绑定（顶点组权重已经算好了）。"""
    mesh_obj.parent = arm_obj
    mesh_obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    mod.use_vertex_groups = True


# --------------------------------------------------------------------------- 动画
# 姿态应用、关键帧、Action 兼容、动画片段库全部来自 rig_lib，
# 这样 auto_rig.py 给 AI 生成的网格套骨架时，能直接复用同一批动画。

#: 本生成器默认带上的片段（保持向后兼容：hero 资产一直是这三个）
CLIPS = ["Idle", "Walk", "Attack"]

# 薄封装，保持原有调用点不变
set_pose = rig_lib.set_pose
key_all = rig_lib.key_all
make_action = rig_lib.make_action


def build_animations(arm_obj):
    """生成多个 Action，并推入 NLA，让 glTF 导出器能一次导出所有片段。"""
    return rig_lib.build_animations(arm_obj, CLIPS)


# --------------------------------------------------------------------------- 导出
# 导出参数统一在 rig_lib.export_glb 里维护（auto_rig.py 共用）


def export_glb(path, with_anim=True):
    return rig_lib.export_glb(path, with_anim=with_anim)


# --------------------------------------------------------------------------- 主流程


def main():
    args = parse_args()
    purge_scene()

    bone_list = build_skeleton(args.height)
    parts = build_body_boxes(args.height)
    mesh_obj = create_mesh_object(parts, bone_list)
    arm_obj = create_armature(bone_list)
    bind(mesh_obj, arm_obj)

    tri_count = sum(len(p.vertices) - 2 for p in mesh_obj.data.polygons)
    vert_count = len(mesh_obj.data.vertices)
    print("[pipeline] 网格: %d 顶点 / %d 三角面 / %d 材质槽"
          % (vert_count, tri_count, len(mesh_obj.data.materials)))
    print("[pipeline] 骨架: %d 骨骼" % len(arm_obj.data.bones))

    # 校验权重确实覆盖了所有顶点（防止出现「某些顶点没绑定 → 塌到原点」）
    unweighted = 0
    for v in mesh_obj.data.vertices:
        if sum(g.weight for g in v.groups) < 1e-3:
            unweighted += 1
    print("[pipeline] 未绑定权重的顶点: %d" % unweighted)
    if unweighted:
        print("[pipeline] 警告：存在未绑定顶点，GoDot 里会出现撕裂/塌陷")

    clips = []
    if not args.no_anim:
        clips = build_animations(arm_obj)
        print("[pipeline] 动画片段: %s" % ", ".join(a.name for a in clips))

    # 恢复 rest pose 再导出（否则 Godot 里初始姿势是歪的）
    set_pose(arm_obj, {})
    bpy.context.view_layer.update()

    if args.no_export:
        print("[pipeline] --no-export，跳过导出")
        return

    out_file = os.path.join(args.out, "%s.glb" % args.name)
    export_glb(out_file, with_anim=not args.no_anim)

    size_kb = os.path.getsize(out_file) / 1024.0
    print("[pipeline] 导出完成: %s (%.1f KB)" % (out_file, size_kb))

    meta = {
        "name": args.name,
        "generator": "blender-procedural-humanoid",
        "blender": bpy.app.version_string,
        "height_m": args.height,
        "seed": args.seed,
        "vertices": vert_count,
        "triangles": tri_count,
        "materials": [m.name for m in mesh_obj.data.materials],
        "bones": [b["name"] for b in bone_list],
        "clips": [a.name for a in clips],
        "glb": os.path.basename(out_file),
        "glb_bytes": os.path.getsize(out_file),
    }
    meta_path = os.path.join(args.out, "%s.meta.json" % args.name)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print("[pipeline] 元数据: %s" % meta_path)


if __name__ == "__main__":
    main()
