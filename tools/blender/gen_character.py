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


def bone(name, head, tail, parent=None, connect=False):
    """描述一根骨骼（Z-up，单位米，角色面朝 -Y）。"""
    return {
        "name": name, "head": Vector(head), "tail": Vector(tail),
        "parent": parent, "connect": connect,
    }


def build_skeleton(height=1.8):
    """
    人形骨架定义。比例按 1.8m 成年人缩放，命名与主流游戏引擎人形规范对齐，
    后期接 Mixamo / UniRig / Rigify 的产物时可以按名字映射。
    """
    s = height / 1.8  # 统一缩放系数

    def v(x, y, z):
        return (x * s, y * s, z * s)

    bones = [
        # ---- 躯干 ----
        bone("hips",       v(0, 0, 0.98),    v(0, 0, 1.12)),
        bone("spine",      v(0, 0, 1.12),    v(0, 0, 1.28), "hips", True),
        bone("chest",      v(0, 0, 1.28),    v(0, 0, 1.45), "spine", True),
        bone("neck",       v(0, 0, 1.45),    v(0, 0, 1.55), "chest", True),
        bone("head",       v(0, 0, 1.55),    v(0, 0, 1.75), "neck", True),
        # ---- 左臂 ----
        bone("shoulder.L", v(0.035, 0, 1.40), v(0.175, 0, 1.42), "chest"),
        bone("upperarm.L", v(0.175, 0, 1.42), v(0.175, 0, 1.14), "shoulder.L", True),
        bone("forearm.L",  v(0.175, 0, 1.14), v(0.175, 0, 0.90), "upperarm.L", True),
        bone("hand.L",     v(0.175, 0, 0.90), v(0.175, 0, 0.78), "forearm.L", True),
        # ---- 右臂 ----
        bone("shoulder.R", v(-0.035, 0, 1.40), v(-0.175, 0, 1.42), "chest"),
        bone("upperarm.R", v(-0.175, 0, 1.42), v(-0.175, 0, 1.14), "shoulder.R", True),
        bone("forearm.R",  v(-0.175, 0, 1.14), v(-0.175, 0, 0.90), "upperarm.R", True),
        bone("hand.R",     v(-0.175, 0, 0.90), v(-0.175, 0, 0.78), "forearm.R", True),
        # ---- 左腿 ----
        bone("thigh.L",    v(0.09, 0, 0.96),  v(0.10, 0, 0.53), "hips"),
        bone("shin.L",     v(0.10, 0, 0.53),  v(0.105, 0, 0.09), "thigh.L", True),
        bone("foot.L",     v(0.105, 0, 0.09), v(0.105, -0.16, 0.03), "shin.L", True),
        # ---- 右腿 ----
        bone("thigh.R",    v(-0.09, 0, 0.96), v(-0.10, 0, 0.53), "hips"),
        bone("shin.R",     v(-0.10, 0, 0.53), v(-0.105, 0, 0.09), "thigh.R", True),
        bone("foot.R",     v(-0.105, 0, 0.09), v(-0.105, -0.16, 0.03), "shin.R", True),
    ]
    return bones


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

def dist_point_segment(p, a, b):
    ab = b - a
    denom = ab.dot(ab)
    t = 0.0 if denom < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / denom))
    return (p - (a + ab * t)).length


def compute_weights(verts, bone_list, max_influences=3, falloff=1.6):
    """
    按「到骨骼线段的距离」计算蒙皮权重。

    为什么自己算而不用 Blender 的 automatic weights：
      heat-map 自动权重在无头模式下偶尔会静默失败（返回全零或直接报错），
      而距离权重是确定性的、可复现的，对程序化模型足够好。
      AI 生成的真实网格后期会改用 heat map / 骨骼包络，但接口保持一致。
    """
    weights = []  # 每个顶点 -> {bone_name: w}
    segs = [(b["name"], b["head"], b["tail"]) for b in bone_list]
    for v in verts:
        p = Vector(v)
        ds = [(name, max(dist_point_segment(p, h, t), 1e-4)) for name, h, t in segs]
        ds.sort(key=lambda x: x[1])
        chosen = ds[:max_influences]
        raw = [(n, 1.0 / (d ** falloff)) for n, d in chosen]
        total = sum(w for _, w in raw)
        weights.append({n: w / total for n, w in raw})
    return weights


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


def set_pose(arm_obj, pose):
    """pose: {bone: (rx, ry, rz)}，单位弧度。未列出的骨骼归零。"""
    for pb in arm_obj.pose.bones:
        rot = pose.get(pb.name, (0.0, 0.0, 0.0))
        pb.rotation_euler = Euler(rot, "XYZ")


def key_all(arm_obj, frame):
    for pb in arm_obj.pose.bones:
        pb.keyframe_insert("rotation_euler", frame=frame)
        if pb.name == "hips":
            pb.keyframe_insert("location", frame=frame)


def action_fcurves(action):
    """
    取 Action 里的所有 F-Curve。

    Blender 4.x 是 `action.fcurves`；5.0 起换成「分层动作」：
    action.layers[*].strips[*].channelbags[*].fcurves。
    这里同时兼容两种结构，避免升级 Blender 就炸。
    """
    legacy = getattr(action, "fcurves", None)
    if legacy is not None and len(legacy) > 0:
        return list(legacy)
    out = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            bags = getattr(strip, "channelbags", None)
            if bags is None:
                try:
                    cb = strip.channelbag(action.slots[0])
                    bags = [cb] if cb else []
                except Exception:
                    bags = []
            for bag in bags:
                out.extend(list(bag.fcurves))
    return out


def make_action(arm_obj, name, keys):
    """
    keys: [(frame, pose_dict), ...]
    返回创建好的 Action，并把旋转曲线设为线性插值，避免默认贝塞尔导致的过冲。
    """
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()
    arm_obj.animation_data.action = action
    for frame, pose in keys:
        set_pose(arm_obj, pose)
        key_all(arm_obj, frame)
    for fc in action_fcurves(action):
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    return action


R = math.radians


def anim_idle():
    """待机：轻微呼吸起伏 + 手臂自然下垂微摆。"""
    a = {
        "spine": (R(2), 0, 0), "chest": (R(-2), 0, 0),
        "upperarm.L": (R(4), 0, R(6)), "upperarm.R": (R(4), 0, R(-6)),
        "forearm.L": (R(-8), 0, 0), "forearm.R": (R(-8), 0, 0),
    }
    b = {
        "spine": (R(-2), 0, 0), "chest": (R(2), 0, 0),
        "upperarm.L": (R(-2), 0, R(9)), "upperarm.R": (R(-2), 0, R(-9)),
        "forearm.L": (R(-14), 0, 0), "forearm.R": (R(-14), 0, 0),
    }
    return [(1, a), (25, b), (49, a)]


def anim_walk():
    """走路循环：左右腿交替摆动，手臂反向摆。1 秒 24 帧循环。"""
    contact_l = {
        "thigh.L": (R(-28), 0, 0), "shin.L": (R(10), 0, 0), "foot.L": (R(12), 0, 0),
        "thigh.R": (R(24), 0, 0), "shin.R": (R(6), 0, 0), "foot.R": (R(-14), 0, 0),
        "upperarm.L": (R(22), 0, R(4)), "upperarm.R": (R(-24), 0, R(-4)),
        "forearm.L": (R(-18), 0, 0), "forearm.R": (R(-26), 0, 0),
        "spine": (R(3), 0, 0),
    }
    pass_l = {
        "thigh.L": (R(-4), 0, 0), "shin.L": (R(4), 0, 0), "foot.L": (R(0), 0, 0),
        "thigh.R": (R(2), 0, 0), "shin.R": (R(38), 0, 0), "foot.R": (R(-8), 0, 0),
        "upperarm.L": (R(6), 0, R(4)), "upperarm.R": (R(-6), 0, R(-4)),
        "forearm.L": (R(-14), 0, 0), "forearm.R": (R(-16), 0, 0),
        "spine": (R(4), 0, 0),
    }
    contact_r = {
        "thigh.R": (R(-28), 0, 0), "shin.R": (R(10), 0, 0), "foot.R": (R(12), 0, 0),
        "thigh.L": (R(24), 0, 0), "shin.L": (R(6), 0, 0), "foot.L": (R(-14), 0, 0),
        "upperarm.R": (R(22), 0, R(-4)), "upperarm.L": (R(-24), 0, R(4)),
        "forearm.R": (R(-18), 0, 0), "forearm.L": (R(-26), 0, 0),
        "spine": (R(3), 0, 0),
    }
    pass_r = {
        "thigh.R": (R(-4), 0, 0), "shin.R": (R(4), 0, 0), "foot.R": (R(0), 0, 0),
        "thigh.L": (R(2), 0, 0), "shin.L": (R(38), 0, 0), "foot.L": (R(-8), 0, 0),
        "upperarm.R": (R(6), 0, R(-4)), "upperarm.L": (R(-6), 0, R(4)),
        "forearm.R": (R(-14), 0, 0), "forearm.L": (R(-16), 0, 0),
        "spine": (R(4), 0, 0),
    }
    return [(1, contact_l), (7, pass_l), (13, contact_r), (19, pass_r), (25, contact_l)]


def anim_attack():
    """攻击：右臂上举后向前挥出，躯干跟着拧转。"""
    windup = {
        "upperarm.R": (R(-120), 0, R(-20)), "forearm.R": (R(-70), 0, 0),
        "chest": (0, 0, R(-25)), "spine": (0, 0, R(-12)),
        "upperarm.L": (R(-20), 0, R(30)), "forearm.L": (R(-40), 0, 0),
        "thigh.R": (R(-10), 0, 0), "thigh.L": (R(8), 0, 0),
    }
    strike = {
        "upperarm.R": (R(50), 0, R(10)), "forearm.R": (R(-10), 0, 0),
        "chest": (R(8), 0, R(22)), "spine": (R(6), 0, R(10)),
        "upperarm.L": (R(20), 0, R(10)), "forearm.L": (R(-60), 0, 0),
        "thigh.R": (R(14), 0, 0), "thigh.L": (R(-14), 0, 0),
    }
    recover = {
        "upperarm.R": (R(6), 0, R(-6)), "forearm.R": (R(-14), 0, 0),
        "chest": (R(0), 0, R(4)), "spine": (R(2), 0, 0),
        "upperarm.L": (R(4), 0, R(6)), "forearm.L": (R(-14), 0, 0),
    }
    return [(1, recover), (9, windup), (15, strike), (30, recover)]


CLIPS = {"Idle": anim_idle, "Walk": anim_walk, "Attack": anim_attack}


def build_animations(arm_obj):
    """生成多个 Action，并推入 NLA，让 glTF 导出器能一次导出所有片段。"""
    actions = []
    for name, fn in CLIPS.items():
        act = make_action(arm_obj, name, fn())
        actions.append(act)

    # 收尾：清空当前 action，把每个 action 放到独立 NLA 轨道上
    arm_obj.animation_data.action = None
    for act in actions:
        track = arm_obj.animation_data.nla_tracks.new()
        track.name = act.name
        strip = track.strips.new(act.name, int(act.frame_range[0]), act)
        strip.name = act.name
        track.mute = True  # 导出时不影响 rest pose
    return actions


# --------------------------------------------------------------------------- 导出


def export_glb(path, with_anim=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for obj in bpy.context.scene.objects:
        obj.select_set(True)

    kwargs = dict(
        filepath=path,
        export_format="GLB",
        use_selection=False,
        export_apply=True,             # 应用修改器
        export_yup=True,               # Z-up -> Y-up（Godot 需要）
        export_materials="EXPORT",
        export_skins=True,
        export_animations=with_anim,
        export_animation_mode="ACTIONS",   # 每个 Action 一个动画片段
        export_bake_animation=False,
        export_optimize_animation_size=False,
        export_force_sampling=True,        # 采样成逐帧，避免约束/插值丢失
        export_frame_range=False,
        export_def_bones=False,
        export_normals=True,
        export_tangents=False,
        export_texcoords=True,
        export_extras=False,
        export_cameras=False,
        export_lights=False,
        export_anim_single_armature=True,
    )

    # 不同 Blender 版本参数名有增删，按签名过滤，避免因未知参数直接失败
    op = bpy.ops.export_scene.gltf
    valid = set(op.get_rna_type().properties.keys())
    dropped = [k for k in kwargs if k not in valid]
    for k in dropped:
        kwargs.pop(k)
    if dropped:
        print("[pipeline] 当前 Blender 不支持的导出参数已忽略: %s" % ", ".join(dropped))

    op(**kwargs)
    return path


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
