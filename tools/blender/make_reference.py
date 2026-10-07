"""
造一张「图生3D 友好」的干净参考图。

【为什么要专门做这一步】
第一次测试时我直接用了 `render_preview.py` 的产出当参考图，结果是坏的：
生成出来的是一块 2m×2m×0.37m 的平板。原因不是管线有问题，而是那张图
**带骨骼线框叠加层、还有参考地面网格**，完全不在图生3D 模型的训练分布里
（它见过的是干净背景上的单个物体照片）。

换成官方 demo 图后，同样的参数生成出正常的三维物体（跨度比 1.8 而不是 5.3）。

所以本脚本按「图生3D 输入规范」渲染：
  - 纯白背景，没有地面、没有网格、没有骨骼显示
  - 单一物体居中，占画面约 75%
  - 用 MatCap/Studio 光照，明暗关系清晰（模型靠明暗推断凹凸）
  - 输出正方形，尺寸 512–1024（与官方 demo.png 的 500×500 同量级）

用法：
  <python-with-bpy> make_reference.py --glb <模型.glb> --out <参考图.png>
"""

import argparse
import math
import os
import sys
import warnings

# 注意顺序：bpy 必须最先导入，否则 bmesh/mathutils 等子模块不可用
import bpy
from mathutils import Vector

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RUNNING_AS_MODULE = "bpy" in sys.modules

GLTF_HELPER_COLLECTION = "glTF_not_exported"


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    elif RUNNING_AS_MODULE:
        argv = argv[1:]
    else:
        argv = []
    p = argparse.ArgumentParser(description="渲染图生3D 用的干净参考图")
    p.add_argument("--glb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--size", type=int, default=768)
    p.add_argument("--fill", type=float, default=0.75,
                   help="物体占画面的比例，默认 0.75")
    p.add_argument("--yaw", type=float, default=0.0, help="水平旋转角度（度）")
    return p.parse_args(argv)


def is_helper(obj):
    return any(c.name == GLTF_HELPER_COLLECTION for c in obj.users_collection)


def main():
    args = parse_args()
    if not os.path.exists(args.glb):
        print("[ref] 找不到模型: %s" % args.glb)
        return 1

    if not RUNNING_AS_MODULE:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    else:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)

    # 清空后导入模型
    bpy.ops.import_scene.gltf(filepath=args.glb)
    meshes = [o for o in bpy.context.scene.objects
              if o.type == "MESH" and len(o.data.vertices) > 0 and not is_helper(o)]
    if not meshes:
        print("[ref] 模型里没有网格")
        return 1

    # 删掉导入器辅助体，避免它出现在图里
    for o in list(bpy.context.scene.objects):
        if is_helper(o) or o.type in ("LIGHT", "CAMERA"):
            bpy.data.objects.remove(o, do_unlink=True)

    # 可选的朝向调整
    if abs(args.yaw) > 1e-6:
        import mathutils
        rot = mathutils.Euler((0, 0, math.radians(args.yaw)), "XYZ")
        for m in meshes:
            m.rotation_euler = rot

    bpy.context.view_layer.update()

    # 统一包围盒（世界空间）
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    for m in meshes:
        for c in m.bound_box:
            w = m.matrix_world @ Vector(c)
            lo = Vector((min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z)))
            hi = Vector((max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z)))
    center = (lo + hi) / 2.0
    size = hi - lo
    radius = max(size.x, size.y, size.z, 1e-4)
    print("[ref] 包围盒 跨度 %.3f x %.3f x %.3f" % (size.x, size.y, size.z))

    # --- 相机：正交，正面平视（图生3D 最喜欢正视图）---
    cam_data = bpy.data.cameras.new("RefCam")
    cam_data.type = "ORTHO"
    # fill=0.75 表示物体占画面 75%
    cam_data.ortho_scale = radius / args.fill
    cam = bpy.data.objects.new("RefCam", cam_data)
    bpy.context.collection.objects.link(cam)
    # Blender 里角色正面是 -Y，所以相机放在 -Y 侧
    dist = radius * 3.0
    cam.location = center + Vector((0.0, -dist, 0.0))
    cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    bpy.context.scene.camera = cam

    # --- 渲染设置：纯白背景 + 保证有明暗的着色 ---
    #
    # 【为什么不按欧拉角配三点光】
    # 试过两版 SUN 灯布光，渲染出来都是一张**纯平剪影**（正面没被照亮）。
    # 图生3D 靠明暗推断起伏，喂平剪影只能得到一块平板（实测跨度比 5.83）。
    # 与其继续猜光源朝向，不如用 Workbench 的 STUDIO 光照 ——
    # 它是跟着相机走的固定打光方案，一定有明暗，且无头模式下最稳。
    # 再开 cavity（空腔）着色，把几何的凹凸/缝隙强化出来，这对图生3D 很关键。
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = args.size
    scene.render.resolution_y = args.size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"

    shading = scene.display.shading
    shading.light = "STUDIO"            # 固定影棚光，必有明暗
    shading.studio_light = "Default"    # 默认影棚灯位
    shading.color_type = "MATERIAL"     # 用材质底色，保留固有色
    shading.show_cavity = True          # 开启空腔着色：强化凹凸与缝隙
    shading.cavity_type = "BOTH"        # 屏幕空间 + 世界空间
    shading.curvature_ridge_factor = 1.5
    shading.curvature_valley_factor = 1.5
    shading.show_specular_highlight = True
    shading.show_object_outline = False
    shading.show_shadows = False
    scene.display.render_aa = "16"

    # 世界背景设成纯白（Workbench 会用它当背景）
    world = bpy.data.worlds.new("RefWorld")
    world.use_nodes = False
    world.color = (1.0, 1.0, 1.0)
    scene.world = world
    # 视图着色里的背景类型也设成世界色
    shading.background_type = "WORLD"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # 注意：必须传绝对路径。Blender 解析相对路径时用的是「它自己的工作目录」，
    # 不是调用方 PowerShell 的 cwd —— 实测相对路径会被写到 C:\tools\... 下面。
    out_abs = os.path.abspath(args.out)
    scene.render.filepath = out_abs
    bpy.ops.render.render(write_still=True)

    if os.path.exists(out_abs):
        print("[ref] 完成: %s (%.1f KB, %dx%d)"
              % (out_abs, os.path.getsize(out_abs) / 1024.0, args.size, args.size))
        return 0
    print("[ref] 渲染未产出文件: %s" % out_abs)
    return 1


if __name__ == "__main__":
    sys.exit(main())
