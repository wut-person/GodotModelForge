extends Node3D
##
## GodotModelForge 主场景脚本。
##
## 这个场景同时是：
##   1) 人工打开 Godot 编辑器时的观察窗口（能看到生成出来的角色在转圈）
##   2) DSH 无头验证的入口（`godot --headless --path . -- --selftest`）
##
## 命令行用法（注意 `--` 之后的参数才是本项目自己的参数）：
##   godot --headless --path . -- --selftest                 检查默认角色资产
##   godot --headless --path . -- --check=res://assets/xx.glb 检查指定资产
##   godot --headless --path . -- --selftest --json          以 JSON 输出结果
##
## 退出码：0 = 通过，1 = 失败（便于 CI / 脚本判断）
##

const DEFAULT_CHARACTER := "res://assets/characters/hero/hero.glb"

@export_file("*.glb", "*.gltf", "*.tscn", "*.scn") var asset_path: String = DEFAULT_CHARACTER
@export var spin_speed: float = 0.6
@export var min_bones: int = 1
@export var min_clips: int = 1

var _pivot: Node3D
var _character: Node3D


func _ready() -> void:
	var opts := _parse_args()
	if opts.has("asset"):
		asset_path = opts["asset"]
	if opts.has("min_bones"):
		min_bones = int(opts["min_bones"])
	if opts.has("min_clips"):
		min_clips = int(opts["min_clips"])

	_build_stage()

	if opts.get("selftest", false) or opts.get("check", false):
		var code := _run_selftest(opts.get("json", false))
		get_tree().quit(code)
		return

	_load_character(asset_path)


# ---------------------------------------------------------------- 参数解析

func _parse_args() -> Dictionary:
	var opts := {}
	for raw in OS.get_cmdline_user_args():
		var arg := String(raw)
		if not arg.begins_with("--"):
			continue
		var body := arg.substr(2)
		var parts := body.split("=", true, 1)
		var key := parts[0]
		var val := parts[1] if parts.size() > 1 else ""
		match key:
			"selftest":
				opts["selftest"] = true
				opts["check"] = true
				if not val.is_empty():
					opts["asset"] = val
			"check":
				opts["check"] = true
				if not val.is_empty():
					opts["asset"] = val
			"asset":
				opts["asset"] = val
			"json":
				opts["json"] = true
			"min-bones":
				opts["min_bones"] = val
			"min-clips":
				opts["min_clips"] = val
			_:
				push_warning("未知参数: %s" % arg)
	return opts


# ---------------------------------------------------------------- 场景搭建

func _build_stage() -> void:
	# 灯光
	var light := DirectionalLight3D.new()
	light.name = "KeyLight"
	light.rotation_degrees = Vector3(-48.0, -35.0, 0.0)
	light.light_energy = 1.1
	light.shadow_enabled = true
	add_child(light)

	var fill := DirectionalLight3D.new()
	fill.name = "FillLight"
	fill.rotation_degrees = Vector3(-20.0, 140.0, 0.0)
	fill.light_energy = 0.35
	add_child(fill)

	# 环境（避免无头/正常运行时一片死黑）
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color(0.13, 0.14, 0.17)
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color(0.5, 0.55, 0.65)
	env.ambient_light_energy = 0.6
	var we := WorldEnvironment.new()
	we.name = "WorldEnvironment"
	we.environment = env
	add_child(we)

	# 相机
	var cam := Camera3D.new()
	cam.name = "Camera3D"
	cam.position = Vector3(0.0, 1.4, 3.6)
	cam.rotation_degrees = Vector3(-8.0, 0.0, 0.0)
	cam.fov = 45.0
	add_child(cam)

	# 参考地面（1m 网格，方便肉眼判断模型比例是否正确）
	var grid := MeshInstance3D.new()
	grid.name = "GroundRef"
	var plane := PlaneMesh.new()
	plane.size = Vector2(4.0, 4.0)
	grid.mesh = plane
	var gmat := StandardMaterial3D.new()
	gmat.albedo_color = Color(0.22, 0.23, 0.26)
	grid.material_override = gmat
	add_child(grid)

	_pivot = Node3D.new()
	_pivot.name = "CharacterPivot"
	add_child(_pivot)


# ---------------------------------------------------------------- 资产加载

func _load_character(path: String) -> bool:
	if path.is_empty():
		return false
	if not ResourceLoader.exists(path):
		push_warning("资产不存在: %s" % path)
		return false
	var packed := load(path)
	if not (packed is PackedScene):
		push_warning("资产不是 PackedScene，无法实例化: %s" % path)
		return false
	var inst: Node = (packed as PackedScene).instantiate()
	if inst == null:
		push_warning("资产实例化为空: %s" % path)
		return false

	_character = Node3D.new()
	_character.name = "Character"
	_character.add_child(inst)
	_pivot.add_child(_character)

	# 自动播放第一个动画片段，便于肉眼确认动画是否有效
	var players := AssetInspector.find_all(inst, "AnimationPlayer")
	for p in players:
		var ap := p as AnimationPlayer
		var list := ap.get_animation_list()
		if list.size() > 0:
			ap.play(String(list[0]))
			break
	return true


func _process(delta: float) -> void:
	if _pivot != null:
		_pivot.rotate_y(spin_speed * delta)


# ---------------------------------------------------------------- 自检

func _run_selftest(as_json: bool) -> int:
	var result := {"asset": asset_path, "exists": false, "ok": false, "errors": [], "info": {}}

	if not ResourceLoader.exists(asset_path):
		result["errors"] = ["资产文件不存在或未被 Godot 识别: %s" % asset_path]
		# 额外排查：文件是否物理存在但没被导入
		var abs := ProjectSettings.globalize_path(asset_path)
		if FileAccess.file_exists(asset_path):
			result["errors"].append("文件存在但 Godot 未导入，请先跑一次无头导入（godot --headless --import）")
		else:
			result["errors"].append("磁盘上也没有这个文件: %s" % abs)
		_emit(result, as_json)
		return 1

	result["exists"] = true
	var packed := load(asset_path)
	if not (packed is PackedScene):
		result["errors"] = ["加载结果不是 PackedScene: %s" % str(typeof(packed))]
		_emit(result, as_json)
		return 1

	var inst: Node = (packed as PackedScene).instantiate()
	var report := AssetInspector.inspect(inst, min_bones, min_clips)
	result["ok"] = report["ok"]
	result["errors"] = report["errors"]
	result["info"] = report["info"]
	inst.free()

	_emit(result, as_json)
	return 0 if result["ok"] else 1


func _emit(result: Dictionary, as_json: bool) -> void:
	if as_json:
		print(JSON.stringify(result))
		return
	print("================ 资产自检 ================")
	print("资产: %s" % result["asset"])
	print("存在: %s" % ("是" if result["exists"] else "否"))
	var info: Dictionary = result.get("info", {})
	if not info.is_empty():
		print("网格实例: %s   面(表面)数: %s   顶点数: %s" % [info.get("mesh_instances", 0), info.get("surfaces", 0), info.get("vertices", 0)])
		print("骨架: %s   骨骼数: %s   已蒙皮: %s" % [info.get("skeletons", 0), info.get("bones", 0), info.get("has_skin", false)])
		print("动画片段: %s" % str(info.get("clips", [])))
		print("材质: %s" % str(info.get("materials", [])))
	var errors: Array = result.get("errors", [])
	if errors.is_empty():
		print("结果: 通过 ✅")
	else:
		print("结果: 失败 ❌")
		for e in errors:
			print("  - %s" % e)
	print("==========================================")
