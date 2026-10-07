extends SceneTree
##
## Godot 渲染验收工具：用真实渲染驱动跑若干帧后截图存 PNG。
##
## 为什么需要它：无头自检只能证明「数据是对的」（网格/骨骼/动画都在），
## 证明不了「画面对不对」（比例、朝向、光照、材质）。本脚本负责后者，
## 产出的图供人肉眼确认，默认放 docs/preview/。
##
## 用法（注意 --script 的脚本必须继承 SceneTree 或 MainLoop）：
##   godot --path . --script res://tools/godot/screenshot.gd -- --screenshot=docs/preview/scene.png
##
## 参数：
##   --screenshot=<路径>  输出 PNG，相对路径按项目根解析，默认 docs/preview/godot_scene.png
##   --frames=<N>         预热帧数，默认 45（等光照/阴影/动画稳定下来）
##   --scene=<res://..>   要截的场景，默认 res://scenes/Main.tscn

const DEFAULT_FRAMES := 45
const DEFAULT_OUT := "docs/preview/godot_scene.png"
const DEFAULT_SCENE := "res://scenes/Main.tscn"

var _out_path := DEFAULT_OUT
var _scene_path := DEFAULT_SCENE
var _frames := DEFAULT_FRAMES
var _count := 0
var _captured := false


func _initialize() -> void:
	for raw in OS.get_cmdline_user_args():
		var arg := String(raw)
		if arg.begins_with("--screenshot="):
			_out_path = arg.substr("--screenshot=".length())
		elif arg.begins_with("--frames="):
			_frames = int(arg.substr("--frames=".length()))
		elif arg.begins_with("--scene="):
			_scene_path = arg.substr("--scene=".length())

	if not ResourceLoader.exists(_scene_path):
		push_error("[screenshot] 找不到场景: %s" % _scene_path)
		quit(1)
		return

	var packed := load(_scene_path)
	if not (packed is PackedScene):
		push_error("[screenshot] 不是 PackedScene: %s" % _scene_path)
		quit(1)
		return

	root.add_child((packed as PackedScene).instantiate())
	print("[screenshot] 已加载 %s，预热 %d 帧后截图 -> %s" % [_scene_path, _frames, _out_path])


func _process(_delta: float) -> bool:
	if _captured:
		return true
	_count += 1
	if _count < _frames:
		return false
	_captured = true
	return _capture()


func _capture() -> bool:
	var img: Image = root.get_texture().get_image()
	if img == null:
		push_error("[screenshot] 拿不到 viewport 图像（headless 模式不产生画面，必须用真实渲染驱动）")
		quit(1)
		return true

	var abs_path := _out_path
	if not abs_path.is_absolute_path():
		abs_path = ProjectSettings.globalize_path("res://").path_join(abs_path)
	DirAccess.make_dir_recursive_absolute(abs_path.get_base_dir())

	var err := img.save_png(abs_path)
	if err != OK:
		push_error("[screenshot] 保存失败 err=%d -> %s" % [err, abs_path])
		quit(1)
		return true

	print("[screenshot] 已保存: %s (%dx%d)" % [abs_path, img.get_width(), img.get_height()])
	quit(0)
	return true
