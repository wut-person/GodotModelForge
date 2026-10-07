"""
自动绑定（auto-rig）：把一个「静态网格」套上标准人形骨架并补上动画，导出可直接给 Godot 用的 GLB。

这是整条生成链路的关键收尾工序。因为无论是图生3D（image-to-3D）还是文生3D，
产出都只有**一个静态三角网格**：没有骨骼、没有蒙皮、没有动画。
而用户要的是「3D 角色/怪物（带动画骨骼）」。本脚本负责把前者变成后者。

工作流程：
    1. 载入任意来源的 GLB / OBJ（或直接接收当前场景里的网格对象）
    2. 分析网格体型：总高、脚底、髋宽、肩宽、手臂伸展方向、腿是否分开
    3. 按分析结果把 rig_lib 的标准人形骨架「贴合」到网格上
    4. 用距离权重算法算蒙皮权重（带左右串扰惩罚）
    5. 套用 rig_lib 的动画库（Idle/Walk/Run/Attack）
    6. 导出 GLB

用法：
    <python-with-bpy> auto_rig.py --in <输入.glb> --out <输出.glb> [--height 1.8] [--clips Idle,Walk]
"""

import argparse
import json
import os
import sys
import warnings

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rig_lib
import mesh_ops

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RUNNING_AS_MODULE = "bpy" in sys.modules


# --------------------------------------------------------------------------- 参数

def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    elif RUNNING_AS_MODULE:
        argv = argv[1:]
    else:
        argv = []
    p = argparse.ArgumentParser(description="给静态网格自动套人形骨架并加动画")
    p.add_argument("--in", dest="src", required=True, help="输入 GLB/OBJ 路径（或 'scene' 表示用当前场景）")
    p.add_argument("--out", required=True, help="输出 GLB 路径")
    p.add_argument("--height", type=float, default=0.0,
                   help="目标身高（米）。0 = 用网格自身高度")
    p.add_argument("--clips", default="Idle,Walk,Run,Attack",
                   help="要套用的动画片段，逗号分隔；none 表示只要骨骼不要动画")
    p.add_argument("--side-penalty", type=float, default=2.5,
                   help="左右串扰惩罚系数，越大越不容易出现跨侧影响")
    p.add_argument("--report", default="", help="把分析结果写成 JSON 到该路径（可选）")
    p.add_argument("--prepare", action="store_true",
                   help="先把网格重拓扑/清理/减面/展 UV 再绑定（AI 生成的三角汤必须开）")
    p.add_argument("--target-faces", type=int, default=8000,
                   help="--prepare 时的目标三角面数，默认 8000")
    p.add_argument("--remesh", default="voxel", choices=["voxel", "quad", "none"],
                   help="重拓扑方式：voxel(快) / quad(慢但拓扑好) / none")
    p.add_argument("--uv", default="smart", choices=["smart", "keep", "none"],
                   help="UV 处理：smart(自动展开) / keep(保留原 UV) / none")
    p.add_argument("--strip-rig", action="store_true",
                   help="输入自带骨骼/动画时，先剥掉再重新绑定")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- 网格收集

def clean_scene(import_path=None):
    if not RUNNING_AS_MODULE and import_path is None:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    else:
        for obj in list(bpy.data.objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
    for block in (bpy.data.meshes, bpy.data.armatures, bpy.data.materials,
                  bpy.data.actions, bpy.data.images):
        for item in list(block):
            try:
                block.remove(item)
            except Exception:
                pass


def import_source(src):
    """导入外部模型，返回导入进来的对象列表。"""
    if src.lower() == "scene":
        return list(bpy.context.scene.objects)
    if not os.path.exists(src):
        raise FileNotFoundError("找不到输入文件: %s" % src)
    before = set(bpy.data.objects)
    ext = os.path.splitext(src)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=src)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=src)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=src)
    else:
        raise ValueError("不支持的输入格式: %s（支持 glb/gltf/obj/fbx）" % ext)
    return [o for o in bpy.data.objects if o not in before]


def gather_mesh(objects):
    """
    把导入进来的所有网格合并成一个网格对象。

    AI 生成的结果经常被拆成多个对象/多个材质块，统一合并后处理更省事。
    """
    meshes = [o for o in objects if o.type == "MESH" and len(o.data.vertices) > 0]
    if not meshes:
        raise ValueError("输入里没有任何带顶点的网格")

    # 去掉导入器造的辅助体
    meshes = [o for o in meshes
              if not any(c.name == "glTF_not_exported" for c in o.users_collection)]
    if not meshes:
        raise ValueError("输入里只有导入器辅助体，没有真正的网格")

    if len(meshes) == 1:
        target = meshes[0]
    else:
        bpy.ops.object.select_all(action="DESELECT")
        for o in meshes:
            o.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        with bpy.context.temp_override(active_object=meshes[0], selected_objects=meshes,
                                       selected_editable_objects=meshes):
            bpy.ops.object.join()
        target = meshes[0]

    target.name = "CharacterMesh"
    return target


# --------------------------------------------------------------------------- 体型分析

def vertex_array(obj):
    """取世界空间顶点坐标 (N,3) float64。"""
    n = len(obj.data.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(n, 3)
    m = np.array(obj.matrix_world.to_4x4(), dtype=np.float64)
    co_h = np.hstack([co, np.ones((n, 1))])
    return (co_h @ m.T)[:, :3]


def band_profile(co, z_rel, z_min, height, bins=40):
    """
    沿高度方向切片，统计每层的 X/Y 跨度与中心。

    为什么要同时看 X 和 Y 跨度：
      躯干是「又宽又厚」的，手臂是「伸得远但很薄」的。
      只看 X 跨度会把手臂合并进躯干宽度（自指错误：用躯干宽度去判断手臂，
      而躯干宽度本身又被手臂撑大了），加上 Y 跨度就能把两者区分开。
    """
    profile = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = (z_rel >= lo) & (z_rel < hi)
        if sel.sum() == 0:
            profile.append(None)
            continue
        xs, ys = co[sel, 0], co[sel, 1]
        profile.append({
            "z_rel": (lo + hi) / 2.0,
            "z_abs": z_min + (lo + hi) / 2.0 * height,
            "x_min": float(xs.min()), "x_max": float(xs.max()),
            "x_extent": float(xs.max() - xs.min()),
            "y_min": float(ys.min()), "y_max": float(ys.max()),
            "y_extent": float(ys.max() - ys.min()),
            "count": int(sel.sum()),
        })
    return profile


def analyze(co):
    """
    从顶点云推断体型参数。返回 dict。

    不用任何机器学习，只用「人形的基本几何特征」：
      - 高度方向切片看宽窄（X 跨度）与厚薄（Y 跨度）
      - 躯干 = 中部最厚的那些层；手臂 = 比躯干宽、但比躯干薄的那些层
    推不出来时用人体比例的经验值兜底，保证任何输入都能出一套合理骨骼。
    """
    z_min, z_max = float(co[:, 2].min()), float(co[:, 2].max())
    height = z_max - z_min
    if height <= 1e-6:
        raise ValueError("网格高度为 0，不是有效模型")

    x_min, x_max = float(co[:, 0].min()), float(co[:, 0].max())
    y_min, y_max = float(co[:, 1].min()), float(co[:, 1].max())
    z_rel = (co[:, 2] - z_min) / height          # 0 = 脚底, 1 = 头顶

    profile = band_profile(co, z_rel, z_min, height, bins=40)

    # ---- 中心线：用腿部中段的水平中心，避免手臂张开把中线拽偏 ----
    leg_band = (z_rel > 0.05) & (z_rel < 0.35)
    if leg_band.sum() >= 8:
        cx = float((co[leg_band, 0].min() + co[leg_band, 0].max()) / 2.0)
        cy = float((co[leg_band, 1].min() + co[leg_band, 1].max()) / 2.0)
    else:
        cx, cy = (x_min + x_max) / 2.0, (y_min + y_max) / 2.0

    # ---- 躯干：在腰~胸区间里找 Y 跨度（厚度）最大的那一层，它一定是躯干 ----
    torso_lo, torso_hi = 0.55, 0.90
    torso_cands = [b for b in profile
                   if b and torso_lo <= b["z_rel"] <= torso_hi and b["count"] >= 4]
    if torso_cands:
        torso_band = max(torso_cands, key=lambda b: b["y_extent"])
        torso_half = torso_band["x_extent"] / 2.0
        torso_depth = torso_band["y_extent"]
        torso_center_x = (torso_band["x_min"] + torso_band["x_max"]) / 2.0
    else:
        torso_half = height * 0.09
        torso_depth = height * 0.09
        torso_center_x = cx
    torso_half = max(torso_half, height * 0.02)

    # ---- 手臂：X 跨度明显超过躯干、且厚度小于躯干（不是躯干的一部分）----
    arm_bins = []
    for b in profile:
        if not b or b["z_rel"] < 0.30 or b["count"] < 3:
            continue
        wider = b["x_extent"] > torso_half * 2.0 * 1.12
        thinner = b["y_extent"] < torso_depth * 1.35
        if wider and thinner:
            arm_bins.append(b)

    has_arms = len(arm_bins) >= 2
    if has_arms:
        arm_bottom = min(b["z_abs"] for b in arm_bins)
        arm_top = max(b["z_abs"] for b in arm_bins)
        # 手臂端点在 X 方向的最远处（左右取绝对值后再取中位数，抗单侧干扰）
        reach = float(np.median([max(abs(b["x_min"] - torso_center_x),
                                     abs(b["x_max"] - torso_center_x))
                                 for b in arm_bins]))
        # 手：手臂最低那一层的中心
        lowest = min(arm_bins, key=lambda b: b["z_abs"])
        hand_x = (lowest["x_min"] + lowest["x_max"]) / 2.0 - torso_center_x
        hand_x = max(abs(hand_x), reach * 0.8)
        hand_y = (lowest["y_min"] + lowest["y_max"]) / 2.0
        hand_z = lowest["z_abs"]
    else:
        arm_bottom = z_min + height * 0.42
        arm_top = z_min + height * 0.78
        reach = height * 0.22
        hand_x = height * 0.20
        hand_y = cy
        hand_z = arm_bottom

    # ---- 髋宽：腿部上方的水平宽度 ----
    hip_band = (z_rel > 0.42) & (z_rel < 0.55)
    if hip_band.sum() > 4:
        hip_width = float(co[hip_band, 0].max() - co[hip_band, 0].min())
    else:
        hip_width = torso_half * 2.0
    # 髋不会比躯干宽太多（除非真的是怪物体型，这里的钳制是为了不让手臂把髋撑开）
    hip_width = min(hip_width, torso_half * 2.6)

    # ---- 腿：脚附近是不是分成两坨（中间有缝）----
    foot_band = (z_rel < 0.08)
    legs_split = False
    if foot_band.sum() >= 6:
        xs = np.sort(co[foot_band, 0])
        gaps = np.diff(xs)
        typical = float(np.median(gaps)) if len(gaps) else 0.0
        legs_split = bool(len(gaps) and gaps.max() > max(typical * 6.0, height * 0.01))

    # 手臂层的判定结果保留下来，fit_skeleton 要用它来定手臂骨骼的 X 位置
    arm_bin_ids = {id(b) for b in arm_bins}
    for b in profile:
        if b is not None:
            b["is_arm"] = id(b) in arm_bin_ids

    widest = max((b for b in profile if b), key=lambda b: b["x_extent"], default=None)

    return {
        "height": height,
        "z_min": z_min,
        "z_max": z_max,
        "center_x": cx,
        "center_y": cy,
        "torso_center_x": torso_center_x,
        "x_extent": x_max - x_min,
        "y_extent": y_max - y_min,
        "hip_width": hip_width,
        "torso_half": torso_half,
        "torso_depth": torso_depth,
        "has_arms": has_arms,
        "arm_span": reach,
        "arm_bottom": arm_bottom,
        "arm_top": arm_top,
        "arm_bins": len(arm_bins),
        "hand_x": hand_x,
        "hand_y": hand_y,
        "hand_z": hand_z,
        "legs_split": legs_split,
        "widest_z_rel": widest["z_rel"] if widest else 0.5,
        "vertex_count": int(co.shape[0]),
        "profile": [b for b in profile if b is not None],
    }


# --------------------------------------------------------------------------- 骨架贴合

def fit_skeleton(info, target_height=None):
    """
    把 rig_lib 的标准人形骨架贴合到实际体型上。

    做法分两步：
      1. 先用「身高 + 身体中心」把整套骨架等比缩放/平移过去（保证不跑偏）
      2. 再用分析出来的实际测量值，覆盖掉关键关节（髋宽、肩高、手臂长度、脚朝向）

    第 2 步是关键：AI 生成的模型经常是「大头短腿」或「长臂怪物」，
    纯等比缩放会让骨骼跑到肉外面去。
    """
    H = info["height"]
    z0 = info["z_min"]
    cx, cy = info["center_x"], info["center_y"]

    bones = rig_lib.skeleton(scaled=True, height=H)

    # 体型越不像人，肩/髋这些经验比例越不可靠，所以用测量值兜底
    hip_width = max(info["hip_width"], H * 0.08)
    hip_half = hip_width / 2.0
    leg_half = hip_half * 0.92

    # 肩高：取手臂高度区间的上沿；没有手臂就按人体比例
    if info["has_arms"]:
        arm_top_rel = (info["arm_top"] - z0) / H
        shoulder_z = z0 + min(max(arm_top_rel, 0.66), 0.88) * H
    else:
        shoulder_z = z0 + 0.79 * H

    hand_x = max(info["hand_x"], hip_half * 1.05)

    # 手臂的 X 位置：优先用实际测量到的「手臂层中心」，这样即使手臂贴着身体
    # （比如垂手站立的人形），骨骼也会落在手臂里而不是插进胸腔。
    arm_cx = None
    prof = info.get("profile") or []
    arm_layers = [b for b in prof if b and b.get("is_arm")]
    if arm_layers:
        left = [b["x_max"] for b in arm_layers]
        right = [b["x_min"] for b in arm_layers]
        # 左右两侧各自取"伸得最远"的那一层的中心，比较稳健
        far_l = max(arm_layers, key=lambda b: b["x_max"])
        far_r = min(arm_layers, key=lambda b: b["x_min"])
        arm_cx = {
            "L": (far_l["x_max"] + max(far_l["x_min"], info["torso_center_x"])) / 2.0,
            "R": (far_r["x_min"] + min(far_r["x_max"], info["torso_center_x"])) / 2.0,
        }
        # 兜底：如果测出来的中心还不如髋宽远，说明测量不可信，用伸展距离反推
        if abs(arm_cx["L"] - cx) < hip_half:
            arm_cx = {"L": cx + hand_x, "R": cx - hand_x}

    # 脚长：沿 Y 方向取模型实际的进深，但不超过 0.12H（避免把披风当地面）
    foot_len = min(info["y_extent"] * 0.55, H * 0.12)
    if foot_len < H * 0.03:
        foot_len = H * 0.09

    by_name = {b["name"]: b for b in bones}

    def set_bone(name, head, tail):
        b = by_name.get(name)
        if b is None:
            return
        b["head"] = Vector(head)
        b["tail"] = Vector(tail)

    # ---- 躯干链：X/Y 对齐到身体中心，高度按人体比例分配 ----
    # 髋关节高度取腿长的实测值（腿有多长，髋就在多高）
    thigh_top = z0 + 0.533 * H
    set_bone("hips", (cx, cy, z0 + 0.545 * H), (cx, cy, z0 + 0.622 * H))
    set_bone("spine", (cx, cy, z0 + 0.622 * H), (cx, cy, z0 + 0.711 * H))
    set_bone("chest", (cx, cy, z0 + 0.711 * H), (cx, cy, shoulder_z))
    # 脖子/头：头部高度用模型顶部反推（头顶就是 z_max）
    neck_z = shoulder_z + (info["z_max"] - shoulder_z) * 0.35
    set_bone("neck", (cx, cy, shoulder_z), (cx, cy, neck_z))
    set_bone("head", (cx, cy, neck_z), (cx, cy, info["z_max"] - H * 0.02))

    # ---- 手臂：从肩高往下降到实际手部位置 ----
    upperarm_top = shoulder_z
    # 上臂/前臂各占手臂长度一半
    arm_len = upperarm_top - info["arm_bottom"]
    if arm_len < H * 0.12:
        arm_len = H * 0.30          # 手臂信息不可靠时的兜底长度
    elbow_z = upperarm_top - arm_len * 0.52
    wrist_z = upperarm_top - arm_len * 0.88
    hand_end_z = upperarm_top - arm_len

    for side, sx in (("L", 1.0), ("R", -1.0)):
        sh_x = hip_half * 0.20 * sx
        if arm_cx is not None:
            up_x = arm_cx[side] - cx
        else:
            up_x = hand_x * sx
        set_bone("shoulder." + side, (cx + sh_x, cy, shoulder_z + H * 0.005),
                 (cx + up_x, cy, upperarm_top))
        set_bone("upperarm." + side, (cx + up_x, cy, upperarm_top),
                 (cx + up_x, cy, elbow_z))
        set_bone("forearm." + side, (cx + up_x, cy, elbow_z),
                 (cx + up_x, cy, wrist_z))
        set_bone("hand." + side, (cx + up_x, cy, wrist_z),
                 (cx + up_x, info["hand_y"], hand_end_z))

    # ---- 腿：从髋到脚，脚朝身体前方（-Y）----
    for side, sx in (("L", 1.0), ("R", -1.0)):
        top_x = cx + hip_half * 0.75 * sx
        bot_x = cx + leg_half * sx
        set_bone("thigh." + side, (top_x, cy, thigh_top), (bot_x, cy, z0 + 0.294 * H))
        set_bone("shin." + side, (bot_x, cy, z0 + 0.294 * H), (bot_x, cy, z0 + 0.05 * H))
        set_bone("foot." + side, (bot_x, cy, z0 + 0.05 * H),
                 (bot_x, cy - foot_len, z0 + 0.017 * H))

    return bones


# --------------------------------------------------------------------------- 建骨架 / 蒙皮

def create_armature(bones, name="Armature"):
    arm_data = bpy.data.armatures.new("CharacterArmature")
    arm_obj = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    try:
        with bpy.context.temp_override(active_object=arm_obj, selected_objects=[arm_obj],
                                       object=arm_obj):
            bpy.ops.object.mode_set(mode="EDIT")
    except Exception:
        bpy.ops.object.mode_set(mode="EDIT")

    created = {}
    for b in bones:
        eb = arm_data.edit_bones.new(b["name"])
        eb.head = b["head"]
        eb.tail = b["tail"]
        eb.roll = 0.0
        created[b["name"]] = eb
    for b in bones:
        if b["parent"] and b["parent"] in created:
            created[b["name"]].parent = created[b["parent"]]
            created[b["name"]].use_connect = bool(b["connect"])
    bpy.ops.object.mode_set(mode="OBJECT")

    for pb in arm_obj.pose.bones:
        pb.rotation_mode = "XYZ"
    return arm_obj


def skin(mesh_obj, arm_obj, bones, side_penalty=2.5):
    """
    算权重并绑定。返回未绑定权重的顶点数（必须为 0，否则 Godot 里会撕裂）。
    """
    co = vertex_array(mesh_obj)
    weights = rig_lib.compute_weights(co.tolist(), bones, max_influences=4,
                                      falloff=1.6, side_penalty=side_penalty)

    # 清掉可能已存在的顶点组，避免重复绑定
    for vg in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vg)

    groups = {}
    for b in bones:
        groups[b["name"]] = mesh_obj.vertex_groups.new(name=b["name"])
    for vi, w in enumerate(weights):
        for bname, val in w.items():
            if val > 1e-4:
                groups[bname].add([vi], val, "REPLACE")

    mesh_obj.parent = arm_obj
    mesh_obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    for mod in list(mesh_obj.modifiers):
        if mod.type == "ARMATURE":
            mesh_obj.modifiers.remove(mod)
    mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    mod.use_vertex_groups = True

    unweighted = 0
    for v in mesh_obj.data.vertices:
        if sum(g.weight for g in v.groups) < 1e-3:
            unweighted += 1
    return unweighted


def strip_existing_rig(mesh_obj, objs):
    """
    剥掉输入自带的骨骼/蒙皮/动画。

    从商店或网上拿到的模型常带一堆用不上的骨骼；直接重新绑定会出现
    「两套骨架打架」，所以先清干净。顶点组也要一并删掉，否则旧权重会残留。
    """
    removed = {"armatures": 0, "modifiers": 0, "vertex_groups": 0, "actions": 0}
    for mod in list(mesh_obj.modifiers):
        if mod.type == "ARMATURE":
            mesh_obj.modifiers.remove(mod)
            removed["modifiers"] += 1
    removed["vertex_groups"] = len(mesh_obj.vertex_groups)
    for vg in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vg)
    mesh_obj.parent = None

    for obj in list(objs):
        if obj is mesh_obj:
            continue
        if obj.type == "ARMATURE":
            bpy.data.objects.remove(obj, do_unlink=True)
            removed["armatures"] += 1
        elif obj.parent is not None and obj.type == "EMPTY":
            # 骨骼层级带出来的空节点也清掉
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass

    for act in list(bpy.data.actions):
        try:
            bpy.data.actions.remove(act)
            removed["actions"] += 1
        except Exception:
            pass
    return removed


# --------------------------------------------------------------------------- 主流程

def auto_rig(src, out_path, target_height=0.0, clips=None, side_penalty=2.5, report_path="",
             prepare=False, target_faces=8000, remesh="voxel", uv="smart", strip_rig=False):
    """
    给一个静态网格套骨架 + 动画并导出。返回分析/结果元数据 dict。

    这个函数也可以被其它脚本直接 import 调用（传 src="scene" 表示用当前场景里的网格）。
    """
    clips = list(rig_lib.clip_names() if clips is None else clips)
    clean_scene(import_path=None if src.lower() == "scene" else src)
    objs = import_source(src)
    mesh_obj = gather_mesh(objs)

    prep_report = None
    if strip_rig:
        removed = strip_existing_rig(mesh_obj, objs)
        print("[autorig] 剥掉原有骨骼: 骨架=%d 修改器=%d 顶点组=%d 动作=%d"
              % (removed["armatures"], removed["modifiers"],
                 removed["vertex_groups"], removed["actions"]))

    # AI 生成的网格是行进立方体出来的三角汤：没有边环、没有 UV、顶点重复，
    # 必须先重拓扑才能绑定骨骼（否则形变会撕裂出尖刺）。
    if prepare:
        print("[autorig] 网格预处理: 目标面数=%d 重拓扑=%s UV=%s"
              % (target_faces, remesh, uv))
        prep_report = mesh_ops.prepare_generated_mesh(
            mesh_obj, target_faces=target_faces, uv=uv, remesh=remesh)
        print("[autorig] 预处理结果: 顶点 %d -> %d, 三角面 %d -> %d, UV=%s"
              % (prep_report["input"]["vertices"], prep_report["output"]["vertices"],
                 prep_report["input"]["triangles"], prep_report["output"]["triangles"],
                 prep_report["output"]["has_uv"]))

    co = vertex_array(mesh_obj)
    info = analyze(co)
    print("[autorig] 分析: 高 %.3f m, 顶点 %d, 髋宽 %.3f, 手臂=%s, 腿分开=%s, 躯干半宽 %.3f"
          % (info["height"], info["vertex_count"], info["hip_width"],
             info["has_arms"], info["legs_split"], info["torso_half"]))

    if not info["has_arms"]:
        print("[autorig] 警告：没检测到明显的手臂伸展，手臂骨骼按人体比例摆放（可能是怪物/非人形）")
    if not info["legs_split"]:
        print("[autorig] 警告：脚部没检测到左右分离，腿部骨骼按髋宽摆放")

    # 目标身高：默认保持模型自身高度（不缩放，避免破坏美术的原意）
    if target_height > 0 and abs(target_height - info["height"]) > 1e-4:
        factor = target_height / info["height"]
        mesh_obj.scale = (factor, factor, factor)
        bpy.context.view_layer.update()
        # 缩放后重新取顶点，骨架按新尺寸贴合
        bpy.context.view_layer.objects.active = mesh_obj
        with bpy.context.temp_override(active_object=mesh_obj, selected_objects=[mesh_obj],
                                       object=mesh_obj):
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        co = vertex_array(mesh_obj)
        info = analyze(co)
        print("[autorig] 已缩放到目标身高 %.3f m" % info["height"])

    bones = fit_skeleton(info, target_height or None)
    arm_obj = create_armature(bones)

    unweighted = skin(mesh_obj, arm_obj, bones, side_penalty=side_penalty)
    print("[autorig] 骨架: %d 骨骼, 未绑定顶点: %d" % (len(bones), unweighted))
    if unweighted:
        print("[autorig] 警告：有未绑定顶点，Godot 里会出现撕裂/塌陷")

    actions = []
    if clips:
        actions = rig_lib.build_animations(arm_obj, clips)
        print("[autorig] 动画片段: %s" % ", ".join(a.name for a in actions))

    rig_lib.set_pose(arm_obj, {})
    bpy.context.view_layer.update()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    rig_lib.export_glb(out_path, with_anim=bool(clips))
    size_kb = os.path.getsize(out_path) / 1024.0
    print("[autorig] 导出: %s (%.1f KB)" % (out_path, size_kb))

    # 三角面数
    tri = sum(len(p.vertices) - 2 for p in mesh_obj.data.polygons)

    # 报告里只保留可读的标量；profile 太啰嗦，单独精简成「哪些高度层被判为手臂」
    analysis = {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in info.items() if k != "profile"}
    analysis["arm_layers_z_rel"] = [round(b["z_rel"], 3)
                                    for b in info.get("profile", []) if b.get("is_arm")]

    meta = {
        "generator": "autorig",
        "source": src,
        "glb": os.path.basename(out_path),
        "glb_bytes": os.path.getsize(out_path),
        "height_m": round(info["height"], 4),
        "vertices": info["vertex_count"],
        "triangles": tri,
        "bones": [b["name"] for b in bones],
        "bone_count": len(bones),
        "clips": [a.name for a in actions],
        "unweighted_vertices": unweighted,
        "analysis": analysis,
        "prepared": bool(prep_report),
    }
    if prep_report:
        meta["prepare"] = {
            "target_faces": target_faces,
            "remesh": remesh,
            "uv": uv,
            "input": prep_report["input"],
            "output": prep_report["output"],
            "steps": [{"step": s["step"],
                       "faces_before": s["faces_before"],
                       "faces_after": s["faces_after"]}
                      for s in prep_report["steps"]],
        }
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        print("[autorig] 报告: %s" % report_path)

    return meta


def main():
    args = parse_args()
    clips = [] if args.clips.strip().lower() == "none" else \
        [c.strip() for c in args.clips.split(",") if c.strip()]
    auto_rig(args.src, args.out, target_height=args.height, clips=clips,
             side_penalty=args.side_penalty, report_path=args.report,
             prepare=args.prepare, target_faces=args.target_faces,
             remesh=args.remesh, uv=args.uv, strip_rig=args.strip_rig)


if __name__ == "__main__":
    main()
