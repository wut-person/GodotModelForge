"""造一个「三角汤」测试样本：高面数 + 顶点打散 + 无 UV + 无骨骼。

用来验证 auto_rig.py --prepare 的重拓扑链路是否真能把 AI 生成风格的密集网格
处理成可绑定的游戏级网格。
"""

import os
import sys
import warnings

# 注意顺序：bpy 必须最先导入。在 bpy 模块模式下，只有 import bpy 之后
# Blender 的运行时才会接进来，bmesh / mathutils 等子模块才可导入（否则报
# ModuleNotFoundError: No module named 'bmesh'）。
import bpy
import bmesh

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rig_lib

FIX = r"D:\deepseek Flash\GodotModelForge\tools\_fixtures"
SRC = os.path.join(FIX, "hero_static.glb")
OUT = os.path.join(FIX, "hero_soup.glb")


def main():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)

    bpy.ops.import_scene.gltf(filepath=SRC)
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and len(o.data.vertices) > 0
              and not any(c.name == "glTF_not_exported" for c in o.users_collection)]
    mesh = meshes[0]
    print("[soup] 原始: %d 顶点 / %d 面" % (len(mesh.data.vertices), len(mesh.data.polygons)))

    # 清掉原有 UV，模拟生成模型没有 UV 的情况
    while mesh.data.uv_layers:
        mesh.data.uv_layers.remove(mesh.data.uv_layers[0])

    bpy.context.view_layer.objects.active = mesh
    mesh.select_set(True)
    for i in range(2):
        m = mesh.modifiers.new("sub%d" % i, "SUBSURF")
        m.levels = 3
        with bpy.context.temp_override(active_object=mesh, selected_objects=[mesh], object=mesh):
            bpy.ops.object.modifier_apply(modifier=m.name)
    m = mesh.modifiers.new("tri", "TRIANGULATE")
    with bpy.context.temp_override(active_object=mesh, selected_objects=[mesh], object=mesh):
        bpy.ops.object.modifier_apply(modifier=m.name)

    # 把每个三角形拆成独立顶点，模拟行进立方体输出的重复顶点
    bm = bmesh.new()
    bm.from_mesh(mesh.data)
    bmesh.ops.split_edges(bm, edges=bm.edges[:])
    bm.to_mesh(mesh.data)
    bm.free()

    tris = sum(len(p.vertices) - 2 for p in mesh.data.polygons)
    print("[soup] 三角汤化: %d 顶点 / %d 三角面 / UV层=%d"
          % (len(mesh.data.vertices), tris, len(mesh.data.uv_layers)))

    for o in list(bpy.data.objects):
        if o.type != "MESH":
            bpy.data.objects.remove(o, do_unlink=True)

    rig_lib.export_glb(OUT, with_anim=False)
    print("[soup] 已导出: %s (%d KB)" % (OUT, os.path.getsize(OUT) // 1024))


if __name__ == "__main__":
    main()
