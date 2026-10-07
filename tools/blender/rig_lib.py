"""
角色骨骼与动画的共享库。

被两个地方复用：
  - gen_character.py   程序化生成人形角色（自带网格）
  - auto_rig.py        给「任意来源的静态网格」套骨架（图生3D / 文生3D 的产物）

集中在这里的原因：骨骼命名、关节比例、动画库、蒙皮权重算法必须是**同一套**，
否则生成的模型和外部导入的模型动画无法互通，Godot 侧也没法复用同一个 AnimationPlayer 逻辑。
"""

import math

from mathutils import Vector, Euler

R = math.radians

# --------------------------------------------------------------------------- 骨骼规范

#: 人形骨架的关节参考位置（角色总高 1.8 m，Z-up，面朝 -Y，单位米）
#: 每项: 骨骼名 -> (head, tail, parent, connect)
HUMANOID_BONES = [
    # ---- 躯干 ----
    ("hips",       (0.000, 0, 0.980), (0.000, 0, 1.120), None,         False),
    ("spine",      (0.000, 0, 1.120), (0.000, 0, 1.280), "hips",       True),
    ("chest",      (0.000, 0, 1.280), (0.000, 0, 1.450), "spine",      True),
    ("neck",       (0.000, 0, 1.450), (0.000, 0, 1.550), "chest",      True),
    ("head",       (0.000, 0, 1.550), (0.000, 0, 1.750), "neck",       True),
    # ---- 左臂 ----
    ("shoulder.L", (0.035, 0, 1.400), (0.175, 0, 1.420), "chest",      False),
    ("upperarm.L", (0.175, 0, 1.420), (0.175, 0, 1.140), "shoulder.L", True),
    ("forearm.L",  (0.175, 0, 1.140), (0.175, 0, 0.900), "upperarm.L", True),
    ("hand.L",     (0.175, 0, 0.900), (0.175, 0, 0.780), "forearm.L",  True),
    # ---- 右臂 ----
    ("shoulder.R", (-0.035, 0, 1.400), (-0.175, 0, 1.420), "chest",      False),
    ("upperarm.R", (-0.175, 0, 1.420), (-0.175, 0, 1.140), "shoulder.R", True),
    ("forearm.R",  (-0.175, 0, 1.140), (-0.175, 0, 0.900), "upperarm.R", True),
    ("hand.R",     (-0.175, 0, 0.900), (-0.175, 0, 0.780), "forearm.R",  True),
    # ---- 左腿 ----
    ("thigh.L",    (0.090, 0, 0.960), (0.100, 0, 0.530), "hips",    False),
    ("shin.L",     (0.100, 0, 0.530), (0.105, 0, 0.090), "thigh.L", True),
    ("foot.L",     (0.105, 0, 0.090), (0.105, -0.16, 0.03), "shin.L", True),
    # ---- 右腿 ----
    ("thigh.R",    (-0.090, 0, 0.960), (-0.100, 0, 0.530), "hips",    False),
    ("shin.R",     (-0.100, 0, 0.530), (-0.105, 0, 0.090), "thigh.R", True),
    ("foot.R",     (-0.105, 0, 0.090), (-0.105, -0.16, 0.03), "shin.R", True),
]

#: 参考高度（HUMANOID_BONES 是按这个身高设计的）
REFERENCE_HEIGHT = 1.8

#: 躯干骨骼（自动适配体型时整体平移的部分）
TORSO_CHAIN = ["hips", "spine", "chest", "neck", "head"]

#: 左右镜像对，自动适配体型时保证左右对称
MIRROR_PAIRS = [
    ("shoulder.L", "shoulder.R"),
    ("upperarm.L", "upperarm.R"),
    ("forearm.L", "forearm.R"),
    ("hand.L", "hand.R"),
    ("thigh.L", "thigh.R"),
    ("shin.L", "shin.R"),
    ("foot.L", "foot.R"),
]


def skeleton(scaled=True, height=1.8):
    """
    返回骨架描述列表，每项是 dict(name, head, tail, parent, connect)。

    scaled=True 时把参考骨架按 height 等比缩放（1.8 m 为基准）。
    """
    s = (height / REFERENCE_HEIGHT) if scaled else 1.0
    out = []
    for name, head, tail, parent, connect in HUMANOID_BONES:
        out.append({
            "name": name,
            "head": Vector((head[0] * s, head[1] * s, head[2] * s)),
            "tail": Vector((tail[0] * s, tail[1] * s, tail[2] * s)),
            "parent": parent,
            "connect": connect,
        })
    return out


# --------------------------------------------------------------------------- 蒙皮权重

def dist_point_segment(p, a, b):
    """点到线段的距离。"""
    ab = b - a
    denom = ab.dot(ab)
    t = 0.0 if denom < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / denom))
    return (p - (a + ab * t)).length


def compute_weights(verts, bones, max_influences=3, falloff=1.6,
                    restrict_to=None, side_penalty=1.0):
    """
    按「到骨骼线段的距离」计算蒙皮权重，返回 [ {bone_name: weight}, ... ]。

    为什么不用 Blender 的 automatic weights：
      heat-map 自动权重在无头模式下会静默失败（返回全零或直接报错），
      而距离权重是确定性的、可复现的，对程序化模型和 AI 生成的网格都够用。

    restrict_to: 可选 {bone_name: vertex_index_predicate}，
                 用来防止明显串扰（例如左手骨骼影响右臂）。
    side_penalty: >1 时对「跨侧」骨骼的距离加权惩罚，抑制左右串扰。
    """
    segs = [(b["name"], b["head"], b["tail"]) for b in bones]
    weights = []
    for vi, v in enumerate(verts):
        p = Vector(v)
        ds = []
        for name, h, t in segs:
            if restrict_to and name in restrict_to and not restrict_to[name](vi):
                continue
            d = max(dist_point_segment(p, h, t), 1e-4)
            # 跨侧惩罚：顶点和骨骼在 X 轴两侧时，距离乘一个系数，降低串扰
            if side_penalty > 1.0:
                if name.endswith(".L") and p.x < -1e-6:
                    d *= side_penalty
                elif name.endswith(".R") and p.x > 1e-6:
                    d *= side_penalty
            ds.append((name, d))
        if not ds:
            ds = [(name, max(dist_point_segment(p, h, t), 1e-4)) for name, h, t in segs]
        ds.sort(key=lambda x: x[1])
        raw = [(n, 1.0 / (d ** falloff)) for n, d in ds[:max_influences]]
        total = sum(w for _, w in raw)
        weights.append({n: w / total for n, w in raw})
    return weights


# --------------------------------------------------------------------------- 动画库

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


def anim_run():
    """跑步循环：比走路幅度更大、有腾空相。24 帧循环。"""
    contact_l = {
        "thigh.L": (R(-45), 0, 0), "shin.L": (R(28), 0, 0), "foot.L": (R(8), 0, 0),
        "thigh.R": (R(38), 0, 0), "shin.R": (R(20), 0, 0), "foot.R": (R(-20), 0, 0),
        "upperarm.L": (R(55), 0, R(6)), "upperarm.R": (R(-60), 0, R(-6)),
        "forearm.L": (R(-70), 0, 0), "forearm.R": (R(-85), 0, 0),
        "spine": (R(10), 0, 0), "chest": (R(4), 0, 0),
    }
    air_l = {
        "thigh.L": (R(-8), 0, 0), "shin.L": (R(75), 0, 0), "foot.L": (R(-6), 0, 0),
        "thigh.R": (R(-12), 0, 0), "shin.R": (R(18), 0, 0), "foot.R": (R(4), 0, 0),
        "upperarm.L": (R(18), 0, R(6)), "upperarm.R": (R(-22), 0, R(-6)),
        "forearm.L": (R(-78), 0, 0), "forearm.R": (R(-72), 0, 0),
        "spine": (R(12), 0, 0), "chest": (R(3), 0, 0),
    }
    contact_r = {
        "thigh.R": (R(-45), 0, 0), "shin.R": (R(28), 0, 0), "foot.R": (R(8), 0, 0),
        "thigh.L": (R(38), 0, 0), "shin.L": (R(20), 0, 0), "foot.L": (R(-20), 0, 0),
        "upperarm.R": (R(55), 0, R(-6)), "upperarm.L": (R(-60), 0, R(6)),
        "forearm.R": (R(-70), 0, 0), "forearm.L": (R(-85), 0, 0),
        "spine": (R(10), 0, 0), "chest": (R(4), 0, 0),
    }
    air_r = {
        "thigh.R": (R(-8), 0, 0), "shin.R": (R(75), 0, 0), "foot.R": (R(-6), 0, 0),
        "thigh.L": (R(-12), 0, 0), "shin.L": (R(18), 0, 0), "foot.L": (R(4), 0, 0),
        "upperarm.R": (R(18), 0, R(-6)), "upperarm.L": (R(-22), 0, R(6)),
        "forearm.R": (R(-78), 0, 0), "forearm.L": (R(-72), 0, 0),
        "spine": (R(12), 0, 0), "chest": (R(3), 0, 0),
    }
    return [(1, contact_l), (7, air_l), (13, contact_r), (19, air_r), (25, contact_l)]


#: 内置动画片段库：名字 -> 生成关键帧的函数
CLIP_BUILDERS = {
    "Idle": anim_idle,
    "Walk": anim_walk,
    "Run": anim_run,
    "Attack": anim_attack,
}


def clip_names():
    return list(CLIP_BUILDERS.keys())


def build_clip(name):
    """按名字取关键帧序列 [(frame, pose_dict), ...]。"""
    if name not in CLIP_BUILDERS:
        raise KeyError("没有这个动画片段: %s（可用: %s）" % (name, ", ".join(clip_names())))
    return CLIP_BUILDERS[name]()


# --------------------------------------------------------------------------- Blender 辅助

def set_pose(arm_obj, pose):
    """把 pose（{骨骼: (rx, ry, rz)} 弧度）应用到骨架；未列出的骨骼归零。"""
    for pb in arm_obj.pose.bones:
        rot = pose.get(pb.name, (0.0, 0.0, 0.0))
        pb.rotation_euler = Euler(rot, "XYZ")


def key_all(arm_obj, frame):
    """给所有骨骼打旋转关键帧，给 hips 额外打位移关键帧。"""
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
    返回创建好的 Action，并把插值设为线性，避免默认贝塞尔导致的过冲。
    """
    import bpy
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


def build_animations(arm_obj, names=None):
    """
    为一根骨架生成多个 Action 并推入 NLA，让 glTF 导出器能一次导出所有片段。
    names 为 None 时使用全部内置片段。
    """
    names = clip_names() if names is None else names
    actions = []
    for name in names:
        actions.append(make_action(arm_obj, name, build_clip(name)))

    # 收尾：清空当前 action，把每个 action 放到独立 NLA 轨道上
    if arm_obj.animation_data:
        arm_obj.animation_data.action = None
        for act in actions:
            track = arm_obj.animation_data.nla_tracks.new()
            track.name = act.name
            strip = track.strips.new(act.name, int(act.frame_range[0]), act)
            strip.name = act.name
            track.mute = True  # 导出时不影响 rest pose
    return actions


def export_glb(path, with_anim=True, extra=None):
    """
    导出 GLB，参数按 Godot 4 的导入需求调好。

    不同 Blender 版本参数名有增删，所以按操作符签名过滤未知参数，避免直接失败。
    """
    import bpy

    os_dir = __import__("os").path.dirname(path)
    if os_dir:
        __import__("os").makedirs(os_dir, exist_ok=True)

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
        export_rest_position_armature=True,
    )
    if extra:
        kwargs.update(extra)

    op = bpy.ops.export_scene.gltf
    valid = set(op.get_rna_type().properties.keys())
    dropped = [k for k in kwargs if k not in valid]
    for k in dropped:
        kwargs.pop(k)
    if dropped:
        print("[rig_lib] 当前 Blender 不支持的导出参数已忽略: %s" % ", ".join(dropped))

    op(**kwargs)
    return path
