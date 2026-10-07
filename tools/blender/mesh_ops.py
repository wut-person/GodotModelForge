"""
网格清理与重拓扑工具。

为什么需要这一步：
  所有图生3D / 文生3D 模型都是「隐式场 + 行进立方体/对偶轮廓」提取出来的，
  产物是**密集三角汤**：没有边环（edge loop）、没有形变极点、没有 UV、顶点重复。
  这种网格直接进游戏有三个问题：
    1. 面数爆炸（常见 10 万+），手机/中端显卡跑不动
    2. 没法绑定骨骼做动画（三角汤形变会撕裂、出现尖刺）
    3. 没有 UV，贴图会拉伸成一团

  所以「生成 → 减面/重拓扑 → UV → 绑定」是必须的工序，本文件负责前三步。

技术要点（都是踩过的坑）：
  - `decimate_type` 的合法值是 COLLAPSE / UNSUBDIV / DISSOLVE。
    界面上那个叫 "Planar" 的选项，代码里是 DISSOLVE —— 传 'PLANAR' 会直接抛异常。
  - `voxel_remesh` 的体素大小**不是操作符参数**，要写到 mesh 数据块上
    （`mesh.remesh_voxel_size`）再调操作符。
  - `smart_project` 必须在**编辑模式**下调用（内部要 BMEditMesh）。
  - 顶点焊接用 `remove_doubles`，是编辑模式操作。
"""

# 注意顺序：bpy 必须最先导入。在 bpy 模块模式下，只有 import bpy 之后
# Blender 的运行时才会接进来，bmesh / mathutils 等子模块才可导入
# （否则报 ModuleNotFoundError: No module named 'bmesh'）。
import bpy
import bmesh


# --------------------------------------------------------------------------- 基础工具

def object_stats(obj):
    """返回网格统计信息。"""
    me = obj.data
    tris = sum(len(p.vertices) - 2 for p in me.polygons)
    quads = sum(1 for p in me.polygons if len(p.vertices) == 4)
    ngons = sum(1 for p in me.polygons if len(p.vertices) > 4)
    return {
        "vertices": len(me.vertices),
        "polygons": len(me.polygons),
        "triangles": tris,
        "quads": quads,
        "ngons": ngons,
        "materials": len(me.materials),
        "uv_layers": len(me.uv_layers),
        "has_uv": len(me.uv_layers) > 0,
    }


def _enter_edit(obj):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
            bpy.ops.object.mode_set(mode="EDIT")
    except Exception:
        bpy.ops.object.mode_set(mode="EDIT")


def _exit_edit(obj):
    try:
        with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        bpy.ops.object.mode_set(mode="OBJECT")


def apply_modifiers(obj):
    """应用对象上的所有修改器（导出前调用，避免 glTF 里带未应用的修改器）。"""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    for mod in list(obj.modifiers):
        try:
            with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
                bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception as exc:
            print("[mesh_ops] 修改器 %s 应用失败（忽略）: %s" % (mod.name, exc))


# --------------------------------------------------------------------------- 清理步骤

def weld(obj, merge_dist=0.0001):
    """
    合并重复顶点。

    图生3D 的输出经常有大量重复顶点（每个三角形独立顶点），
    不焊接的话后面的体素重拓扑和权重计算都会出问题。
    """
    before = len(obj.data.vertices)
    _enter_edit(obj)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=merge_dist)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    _exit_edit(obj)
    after = len(obj.data.vertices)
    return {"vertices_before": before, "vertices_after": after, "merged": before - after}


def cleanup(obj, fill_holes=True, remove_loose=True):
    """去松散点/边、填补小洞、统一法线 —— 让网格变成封闭流形。"""
    stats = {}
    _enter_edit(obj)
    bpy.ops.mesh.select_all(action="SELECT")
    if remove_loose:
        try:
            bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)
        except Exception as exc:
            stats["delete_loose"] = "skip: %s" % exc
    if fill_holes:
        try:
            bpy.ops.mesh.fill_holes(sides=8)
            stats["filled_holes"] = True
        except Exception as exc:
            stats["filled_holes"] = "skip: %s" % exc
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    _exit_edit(obj)
    return stats


def voxel_remesh(obj, voxel_size=0.0, adaptivity=0.0, auto_target_faces=20000):
    """
    体素重网格化：把三角汤变成均匀、封闭、连贯的流形表面。

    这是让 AI 生成网格「能绑定骨骼」的关键一步 —— 体素化会重建出均匀的拓扑，
    消除行进立方体留下的细碎三角和自交。

    voxel_size=0 时按目标面数自动估算（体素越小面越多）。
    注意：体素大小要写在 mesh 数据块上，不是操作符参数。
    """
    me = obj.data
    if voxel_size <= 0:
        # 体素边长约为「包围盒对角线 / 目标密度」，经验系数按目标面数反推
        dims = obj.dimensions
        diag = max((dims.x ** 2 + dims.y ** 2 + dims.z ** 2) ** 0.5, 1e-4)
        # 经验关系：表面面积 ~ diag²，面数 ~ (diag / voxel)²，故 voxel ≈ diag / sqrt(faces)
        voxel_size = diag / max((auto_target_faces ** 0.5), 1.0)

    me.remesh_voxel_size = float(voxel_size)
    me.remesh_voxel_adaptivity = float(adaptivity)
    me.remesh_mode = "VOXEL"

    before = object_stats(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
            result = bpy.ops.object.voxel_remesh()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "voxel_size": voxel_size, "before": before}
    after = object_stats(obj)
    return {
        "ok": "FINISHED" in result,
        "voxel_size": round(voxel_size, 5),
        "before": before, "after": after,
    }


def quad_remesh(obj, target_faces=8000):
    """
    四边形重网格化（Blender 4.x 的 Quadriflow）。

    产出的四边形网格最利于骨骼形变，但**很慢**（几万面可能要几分钟）。
    Blender 版本不支持时返回 ok=False，让调用方回退到体素 + 减面。
    """
    before = object_stats(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    kwargs = {"mode": "QUAD", "target_faces": int(target_faces)}
    try:
        with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
            result = bpy.ops.object.quadriflow_remesh(**kwargs)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "before": before}
    after = object_stats(obj)
    return {"ok": any("FINISHED" in r for r in result) if result else False,
            "before": before, "after": after, "target_faces": target_faces}


def decimate(obj, ratio=0.5, target_faces=0):
    """
    减面。用 COLLAPSE 模式（唯一适合有机体的模式）。

    注意：decimate_type 的合法值是 COLLAPSE / UNSUBDIV / DISSOLVE，
    界面上的 "Planar" 对应 DISSOLVE，传 'PLANAR' 会抛异常。
    """
    before = object_stats(obj)
    if target_faces > 0 and before["triangles"] > 0:
        ratio = min(1.0, float(target_faces) / float(before["triangles"]))
    ratio = max(0.001, min(1.0, ratio))

    mod = obj.modifiers.new("Decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"      # 不是 'PLANAR'
    mod.ratio = ratio
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
            bpy.ops.object.modifier_apply(modifier=mod.name)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "before": before}
    after = object_stats(obj)
    return {"ok": True, "ratio": round(ratio, 4), "before": before, "after": after}


def shade_smooth(obj, angle_deg=35.0, auto_smooth=True):
    """平滑着色 + 自动锐边：低面数模型的关键观感提升。"""
    me = obj.data
    for poly in me.polygons:
        poly.use_smooth = True
    if auto_smooth:
        # Blender 4.1+ 用修改器实现「按角度自动平滑」
        try:
            if "Smooth by Angle" not in [m.name for m in obj.modifiers]:
                bpy.context.view_layer.objects.active = obj
                obj.select_set(True)
                with bpy.context.temp_override(active_object=obj, selected_objects=[obj], object=obj):
                    bpy.ops.object.shade_auto_smooth(angle=angle_deg * 3.14159265 / 180.0)
                return {"ok": True, "method": "shade_auto_smooth"}
        except Exception:
            pass
        try:
            me.use_auto_smooth = True
            me.auto_smooth_angle = angle_deg * 3.14159265 / 180.0
            return {"ok": True, "method": "use_auto_smooth"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "method": "flat_smooth_only"}


def smart_uv_project(obj, angle_limit_deg=66.0, island_margin=0.02):
    """
    用 Blender 内置的 Smart UV Project 展开 UV。

    必须在**编辑模式**下调用（内部需要 BMEditMesh）。
    """
    if len(obj.data.polygons) == 0:
        return {"ok": False, "error": "网格没有面"}
    _enter_edit(obj)
    bpy.ops.mesh.select_all(action="SELECT")
    try:
        bpy.ops.uv.smart_project(angle_limit=angle_limit_deg * 3.14159265 / 180.0,
                                 island_margin=island_margin)
        ok = True
        err = None
    except Exception as exc:
        ok = False
        err = str(exc)
    _exit_edit(obj)
    return {"ok": ok, "error": err, "uv_layers": len(obj.data.uv_layers)}


def recalc_normals(obj):
    """朝外统一法线：法线反了会导致游戏里模型发黑/被剔除。"""
    _enter_edit(obj)
    bpy.ops.mesh.select_all(action="SELECT")
    try:
        bpy.ops.mesh.normals_make_consistent(inside=False)
        ok = True
    except Exception as exc:
        ok = str(exc)
    _exit_edit(obj)
    return ok


# --------------------------------------------------------------------------- 组合流程

def prepare_generated_mesh(obj, target_faces=8000, uv="smart", remesh="voxel",
                          voxel_size=0.0, weld_first=True, verbose=True):
    """
    把「AI 生成的原始网格」处理成「可绑定、可进游戏的网格」。

    target_faces: 目标三角面数（游戏用角色建议 5k–20k）
    uv:           "smart" 用 Blender Smart UV Project / "keep" 保留原 UV / "none" 不展
    remesh:       "voxel"（快，均匀流形）/ "quad"（慢，四边形拓扑）/ "none"

    返回处理报告 dict。
    """
    report = {"input": object_stats(obj), "steps": []}

    def step(name, fn, **kw):
        before = object_stats(obj)
        res = fn(obj, **kw)
        after = object_stats(obj)
        entry = {"step": name, "result": res,
                 "faces_before": before["triangles"], "faces_after": after["triangles"]}
        report["steps"].append(entry)
        if verbose:
            print("[mesh_ops] %-14s 面数 %6d -> %6d  %s"
                  % (name, before["triangles"], after["triangles"],
                     "" if not isinstance(res, dict) or res.get("ok", True) else "（失败）"))
        return res

    if weld_first:
        step("weld", weld)
    step("cleanup", cleanup)

    # 重拓扑：体素重建均匀流形（推荐），或四边形重拓扑（更慢但拓扑更好）
    if remesh == "quad":
        res = step("quad_remesh", quad_remesh, target_faces=target_faces)
        if not res.get("ok"):
            if verbose:
                print("[mesh_ops] quad_remesh 不可用，回退到 voxel_remesh")
            step("voxel_remesh", voxel_remesh, voxel_size=voxel_size,
                 auto_target_faces=target_faces)
    elif remesh == "voxel":
        step("voxel_remesh", voxel_remesh, voxel_size=voxel_size,
             auto_target_faces=target_faces)

    # 减面到目标
    stats = object_stats(obj)
    if target_faces > 0 and stats["triangles"] > target_faces * 1.05:
        step("decimate", decimate, target_faces=target_faces)

    step("normals", lambda o: {"ok": bool(recalc_normals(o))})
    step("shade", lambda o: shade_smooth(o))

    if uv == "smart":
        step("smart_uv", smart_uv_project)
    elif uv == "keep":
        report["steps"].append({"step": "uv", "result": {"ok": True, "kept": True}})

    report["output"] = object_stats(obj)
    return report
