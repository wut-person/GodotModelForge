"""
用 Blender 渲染一张角色预览图，用于肉眼确认「模型有没有生成对」。

不追求画质，追求「一眼能看出比例、朝向、骨骼姿势是否正确」：
  - Workbench 引擎（不依赖 GPU 光追，无头也能跑）
  - 正交相机自动框住模型包围盒
  - 网格 + 骨骼一起显示，能看出骨骼是否和身体对齐

用法：
  <python-with-bpy> render_preview.py --glb <file.glb> --out <file.png> [--width 640] [--height 640]
"""

import argparse
import math
import os
import sys
import warnings

import bpy
from mathutils import Vector

# 见 gen_character.py：静音 DeprecationWarning，避免 PowerShell 把 stderr 当致命错误
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RUNNING_AS_MODULE = "bpy" in sys.modules


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    elif RUNNING_AS_MODULE:
        argv = argv[1:]
    else:
        argv = []
    p = argparse.ArgumentParser(description="渲染角色预览图")
    p.add_argument("--glb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=640)
    p.add_argument("--show-bones", action="store_true", default=True)
    return p.parse_args(argv)


def purge():
    if not RUNNING_AS_MODULE:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    else:
        for obj in list(bpy.data.objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass


# glTF 导入器会额外造一个 icosphere 放进这个名字的集合里，作为灯光/相机的辅助体。
# 它不属于角色，算进包围盒会把相机拉远、角色缩成一根牙签，必须排除。
GLTF_HELPER_COLLECTION = "glTF_not_exported"


def is_helper(obj):
    return any(c.name == GLTF_HELPER_COLLECTION for c in obj.users_collection)


def world_bounds(objects):
    """
    计算所有「真正带几何」的网格对象的世界空间包围盒。

    排除两类噪音：导入器的辅助体、以及顶点数为 0 的空网格。
    """
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    found = False
    used = []
    for obj in objects:
        if obj.type != "MESH" or len(obj.data.vertices) == 0 or is_helper(obj):
            continue
        used.append(obj)
        for corner in obj.bound_box:
            wc = obj.matrix_world @ Vector(corner)
            lo = Vector((min(lo.x, wc.x), min(lo.y, wc.y), min(lo.z, wc.z)))
            hi = Vector((max(hi.x, wc.x), max(hi.y, wc.y), max(hi.z, wc.z)))
            found = True
    if not found:
        return Vector((-1, -1, 0)), Vector((1, 1, 2)), used
    return lo, hi, used


def main():
    args = parse_args()
    if not os.path.exists(args.glb):
        print("[preview] 找不到文件: %s" % args.glb)
        return 1

    purge()

    # --- 导入 glb ---
    try:
        bpy.ops.import_scene.gltf(filepath=args.glb)
    except Exception as exc:
        print("[preview] 导入失败: %s" % exc)
        return 1

    imported = [o for o in bpy.context.scene.objects]
    meshes = [o for o in imported if o.type == "MESH"]
    arms = [o for o in imported if o.type == "ARMATURE"]
    print("[preview] 导入: %d 网格, %d 骨架" % (len(meshes), len(arms)))
    for m in meshes:
        print("[preview]   网格 %s: %d 顶点" % (m.name, len(m.data.vertices)))

    # 隐藏导入器辅助体，避免出现在图里
    for obj in bpy.context.scene.objects:
        if is_helper(obj):
            obj.hide_render = True
            obj.hide_viewport = True

    lo, hi, used = world_bounds(meshes)
    if not used:
        print("[preview] 没有带几何的网格，无法渲染")
        return 1
    size = hi - lo
    center = (lo + hi) / 2.0
    radius = max(size.x, size.y, size.z, 1e-3)

    # --- 相机：正交，自动框住模型 ---
    cam_data = bpy.data.cameras.new("PreviewCam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 1.12   # 留一点边距
    cam = bpy.data.objects.new("PreviewCam", cam_data)
    bpy.context.collection.objects.link(cam)

    # 站在模型正前方略偏右（Blender 里角色正面是 -Y），接近平视
    dist = radius * 4.0
    cam.location = center + Vector((dist * 0.30, -dist * 0.95, radius * 0.10))
    direction = (center - cam.location).normalized()
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam

    # --- 渲染设置 ---
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = args.out

    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "MATERIAL"      # 用材质底色，能看出材质槽分配是否正确
    shading.show_object_outline = True
    scene.display.render_aa = "8"

    # 显示骨骼，方便确认骨架和身体是否对齐
    for arm in arms:
        arm.show_in_front = True
        arm.data.display_type = "OCTAHEDRAL"

    # 地面高度设为模型底部，方便判断「脚是否踩在原点」
    print("[preview] 包围盒: x[%.2f, %.2f] y[%.2f, %.2f] z[%.2f, %.2f]"
          % (lo.x, hi.x, lo.y, hi.y, lo.z, hi.z))
    print("[preview] 尺寸: %.2f x %.2f x %.2f m" % (size.x, size.y, size.z))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    bpy.ops.render.render(write_still=True)

    if os.path.exists(args.out):
        print("[preview] 完成: %s (%.1f KB)" % (args.out, os.path.getsize(args.out) / 1024.0))
        return 0
    print("[preview] 渲染未产出文件")
    return 1


if __name__ == "__main__":
    sys.exit(main())
