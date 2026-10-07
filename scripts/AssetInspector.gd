extends RefCounted
class_name AssetInspector
##
## 资产自检器：在无头模式下检查一个「生成出来的角色资产」是否符合落地规范。
##
## 用途：生成管线（Blender / 图生3D）产出 .glb 之后，由 tools/verify.ps1 调用本文件，
## 自动确认 Godot 侧真的能拿到 网格 + 骨骼 + 动画，避免「文件生成了但导入是坏的」。
##
## 规范（与 docs/ASSET_SPEC.md 一致）：
##   - 根节点应当是一个 Node3D（glTF 导入后通常是场景根）
##   - 至少一个 MeshInstance3D，且其 mesh 非空
##   - 骨骼动画角色：至少一个 Skeleton3D，骨骼数 >= min_bones
##   - 至少一个 AnimationPlayer，且动画数 >= min_clips
##

const DEFAULT_MIN_BONES := 1
const DEFAULT_MIN_CLIPS := 1


## 递归收集某类型的所有子节点（包含 root 自身）
static func find_all(root: Node, type_name: String) -> Array[Node]:
	var out: Array[Node] = []
	if root == null:
		return out
	if root.is_class(type_name):
		out.append(root)
	for child in root.get_children():
		out.append_array(find_all(child, type_name))
	return out


## 检查一个已实例化的角色场景，返回 {ok: bool, errors: Array[String], info: Dictionary}
static func inspect(instance: Node, min_bones: int = DEFAULT_MIN_BONES, min_clips: int = DEFAULT_MIN_CLIPS) -> Dictionary:
	var errors: Array[String] = []
	var info := {}

	if instance == null:
		return {"ok": false, "errors": ["实例为空，资源可能加载失败"], "info": info}

	# --- 网格 ---
	var meshes := find_all(instance, "MeshInstance3D")
	var total_surfaces := 0
	var total_verts := 0
	for m in meshes:
		var mi := m as MeshInstance3D
		if mi.mesh == null:
			errors.append("MeshInstance3D '%s' 的 mesh 为空" % mi.name)
			continue
		total_surfaces += mi.mesh.get_surface_count()
		for s in mi.mesh.get_surface_count():
			var arrays := mi.mesh.surface_get_arrays(s)
			if arrays.size() > Mesh.ARRAY_VERTEX and arrays[Mesh.ARRAY_VERTEX] != null:
				total_verts += (arrays[Mesh.ARRAY_VERTEX] as PackedVector3Array).size()
	info["mesh_instances"] = meshes.size()
	info["surfaces"] = total_surfaces
	info["vertices"] = total_verts
	if meshes.is_empty():
		errors.append("未找到任何 MeshInstance3D（模型没有网格）")
	elif total_surfaces == 0:
		errors.append("网格存在但面数为 0")

	# --- 骨骼 ---
	var skeletons := find_all(instance, "Skeleton3D")
	var bone_count := 0
	for sk in skeletons:
		bone_count += (sk as Skeleton3D).get_bone_count()
	info["skeletons"] = skeletons.size()
	info["bones"] = bone_count
	if skeletons.is_empty():
		errors.append("未找到 Skeleton3D（模型没有骨骼，无法做骨骼动画）")
	elif bone_count < min_bones:
		errors.append("骨骼数 %d < 要求 %d" % [bone_count, min_bones])

	# --- 蒙皮 ---
	var has_skin := false
	for m in meshes:
		var mi := m as MeshInstance3D
		if mi.skin != null:
			has_skin = true
			break
	if not skeletons.is_empty() and not has_skin:
		errors.append("有骨骼但网格未绑定 skin（导出时漏了蒙皮信息）")
	info["has_skin"] = has_skin

	# --- 动画 ---
	var players := find_all(instance, "AnimationPlayer")
	var clips: Array[String] = []
	for p in players:
		var ap := p as AnimationPlayer
		for name in ap.get_animation_list():
			clips.append(name)
	info["animation_players"] = players.size()
	info["clips"] = clips
	if players.is_empty():
		errors.append("未找到 AnimationPlayer（模型没有动画）")
	elif clips.size() < min_clips:
		errors.append("动画片段数 %d < 要求 %d" % [clips.size(), min_clips])

	# --- 材质 ---
	var mats := find_all(instance, "MeshInstance3D")
	var mat_names: Array[String] = []
	for m in mats:
		var mi := m as MeshInstance3D
		if mi.mesh == null:
			continue
		for s in mi.mesh.get_surface_count():
			var mat := mi.get_active_material(s)
			if mat != null and not mat_names.has(mat.resource_name):
				mat_names.append(mat.resource_name)
	info["materials"] = mat_names

	return {"ok": errors.is_empty(), "errors": errors, "info": info}
