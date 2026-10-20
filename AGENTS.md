# 项目指令 —— GodotModelForge

> 工作区根还有一份 [../AGENTS.md](../AGENTS.md)（环境路径、PS 5.1 红线、跑法）。
> 本文件只写**这个项目内部**的东西。更具体的规则优先。
>
> 本项目**不是独立的 git 仓库** —— `D:\deepseek Flash` 本身没有 `.git`，
> 所以这一层是靠 DSH 的「进入更深目录后补注入」机制加载的，不是靠 git 根发现。

## 1. 这个工程解决什么问题

作者的原话（`README.md`）：

> 这个项目解决的核心问题不是「怎么生成模型」，而是
> **「生成出来的东西怎么可靠地变成 Godot 里能用的资产，并且能被自动验证」**。
> 生成端（程序化建模 / 图生3D / 文生3D）可以随便换，但落地标准和验证必须稳定。

所以做任何改动前先问一句：**这改动让「生成」更强了，还是让「落地 + 验证」更可靠了？**
后者才是本项目的重点。

## 2. 权威文件在哪（不要另起一份）

| 要知道什么 | 看哪里 |
|---|---|
| 资产规范（命名/朝向/骨骼/预算/坑） | `docs/ASSET_SPEC.md` |
| 骨骼名的**唯一权威定义** | `tools/blender/rig_lib.py` ← 代码即规范 |
| 校验规则的实际实现 | `scripts/AssetInspector.gd` |
| 三条入口脚本 | `tools/build.cmd` / `tools/rig.cmd` / `tools/verify.cmd` |
| 生成端可插拔的实现 | `tools/blender/gen_character.py`（程序化）、`tools/pipeline/hunyuan3d_shape.py`（图生3D） |

`docs/ASSET_SPEC.md` 第 3 节明确要求：骨骼名不允许在 `gen_character.py` 和 `auto_rig.py`
里各抄一份，必须都从 `rig_lib.py` 取。改骨骼相关代码时遵守这条。

## 3. 资产落地结构（硬性）

```
res://assets/characters/<资产名>/     ← 一个角色一个目录，目录名 == 资产名
├── <资产名>.glb                       ← 主资产
├── <资产名>.glb.import                ← Godot 自动生成，保留、别手改
├── <资产名>.meta.json                 ← 生成元数据（来源/骨骼/片段清单）
├── <资产名>.preview.png               ← Blender 预览
└── <资产名>_albedo.png / _normal.png / _orm.png   ← 可选贴图，后缀固定
```

命名：全小写 + 下划线，**不要中文、不要空格**（Godot 导出到某些平台中文路径会出问题）。

几何约定：**1 单位 = 1 米**；人形身高 1.6–2.0 m；**面朝 +Z**；原点 = 双脚之间的地面点 (0,0,0)。
Blender 侧是 Z-up、正面 -Y，导出时 `export_yup=True`，到 Godot 正好面朝 +Z。

预算：主角 ≤ 5 万三角面（小怪 1 万 / 移动端 5 千，游戏角色一般 5k–20k）；
材质槽 ≤ 4；每顶点骨骼影响 ≤ 4；贴图 2048² 以内，走 ORM 打包。

## 4. 人形骨架（19 根，顺序即层级）

```
hips → spine → chest → neck → head
              chest → shoulder.L/R → upperarm → forearm → hand
       hips → thigh.L/R → shin → foot
```

硬性要求：骨骼数 ≥ 1（人形建议 15–70）；**不允许有未绑定权重的顶点**
（会出现顶点塌到原点的撕裂；`gen_character.py` 会打印未绑定顶点数，必须为 0）；
骨架根必须是 `Skeleton3D` 祖先且为**单位变换**（不要带缩放）。

动画片段：大驼峰命名 `Idle` / `Walk` / `Run` / `Attack` / `Hit` / `Death`；
循环动作（Idle/Walk/Run）**首尾关键帧必须一致**，否则循环时跳变。

## 5. 自动绑定的能力边界

`auto_rig.py` 靠几何特征推断体型。**输入网格的期望**（`docs/ASSET_SPEC.md` §6.2）：
竖直站立、脚底水平、+Z 为身高方向、人形/类人形、左右大致对称、单一网格。

**能处理的不标准情况**：手臂贴身下垂、大头短腿等夸张比例、脚部并拢。
**不能处理**：没有手臂的怪物（会警告并按人体比例强放手臂骨，此时应 `-Clips none` 或自备骨架）；
四足/多足/飞行（骨架模板只有人形一套，需要新模板）。

AI 生成的「三角汤」（高面数、无 UV、重复顶点）**必须加重拓扑参数**：

```cmd
tools\rig.cmd "D:\ai_out\monster.glb" -Prepare -TargetFaces 8000 -Remesh voxel -Uv smart
```

实测：125 万三角面 / 376 万顶点 / 无 UV → 8000 三角面 / 3962 顶点 / 带 UV，34.9 秒。
左右串扰（左臂动右臂跟着动）时调大 `-SidePenalty`。

## 6. 两条入口的区别（别用错）

| 场景 | 用哪条 |
|---|---|
| 从零造一个测试角色 / 验证链路 | `tools\build.cmd <name> -Force -Preview -Screenshot` |
| **已有静态网格**（图生3D、下载、剥离了骨骼的） | `tools\rig.cmd "<input.glb>" -Name x -Prepare ...` |

图生3D 产物走第二条。`strip_rig.py` 可以把现成带骨骼模型剥成静态网格，用来造回归样本
（`hero.glb` 129 KB → `hero_static.glb` 16.8 KB）。

## 7. 实测数据（别再重新测一遍）

- **图生3D = Hunyuan3D-2mini**，是本机 6 GB 显存下**唯一装得下**的：turbo 权重 3.56 GB + VAE 0.38 GB。
  峰值显存 **4.45 / 6.44 GB**，单次约 **67 秒**（30 步，octree 256），输出 13 万–50 万三角面（需重拓扑）。
- **TRELLIS.2 装不下**：权重合计约 7.9 GB（int8 4.89 + VAE 1.02/0.88 + CLIP-ViT 1.13 GB）。
- DINO 权重适配后 **808/808 张量命中、零形状不匹配**，加载后权重标准差 **0.0135**
  （训练好的 ViT 量级；随机初始化会在 1.0 量级）。诊断脚本：`tools/pipeline/diagnose_dino.py`。

## 8. 当前资产

两个资产在 2026-10-12 双双跑过 `tools\verify.cmd`，**均全绿、EXITCODE=0**（不是自述，是实测）。

| 资产 | 内容 |
|---|---|
| `hero` | 程序化人形，1.8 m，136 顶点 / 204 三角面（Blender 侧），材质 `M_Body`/`M_Limb`/`M_Accent`，19 骨骼，片段 `Idle`/`Walk`/`Attack`，GLB 132,124 字节 |
| `ai_demo` | **自动绑定产物**（来源 `tools/_fixtures/gen/demo_gen.glb`，即图生3D 的中间结果）：输入 160,826 顶点 / 321,652 面 / 无 UV → 输出 4,502 顶点 / 9,000 三角面 / 带 UV；19 骨骼，片段 `Idle`/`Walk`/`Run`/`Attack`；GLB 570,808 字节 |

> ⚠️ **顶点数有两套口径，别拿来互相核对**：`meta.json` 记的是 **Blender 侧**焊点后的顶点数
> （hero 136 / ai_demo 4,502），而 `AssetInspector.gd` 报的是 **Godot 导入后**的顶点数
> （hero **408** / ai_demo **7,280**）—— Godot 会按 UV/法线接缝拆点，所以后者总是更大。两个都对。
>
> ⚠️ 自检输出里的「**面数**」目前不可信（hero 报 3、ai_demo 报 1，显然不是三角面数）——
> 判定以「资产符合规范 / 退出码」为准。修 `AssetInspector.gd` 时留意这里。

### 图生3D 权重（跑生成前先确认在不在）

`D:\Tools\models\hunyuan3d-2mini` —— 23 个文件 / **4.34 GB**
（`hunyuan3d-dit-v2-mini-turbo\model.fp16.safetensors` 3.56 GB + VAE mini 0.399 GB + VAE mini-turbo 0.379 GB）。
不在 `D:\Tools\Hunyuan3D-2`（那只是官方**代码**仓库）。可用环境变量 `MODELS_DIR` 覆盖根目录。

## 9. 场景现状（别误判成有玩法）

`scenes/Main.tscn` 只挂了一个 `Main.gd`（`extends Node3D`）。它做两件事：

1. 人工在 Godot 里打开时的**观察窗口** —— 搭灯光/环境/相机/参考地面，
   加载默认角色、播第一个动画、绕 Y 轴自转（`spin_speed = 0.6`）
2. **无头自检入口** —— `--selftest` / `--check=<path>`，走 `AssetInspector.inspect()`，
   退出码 0/1

**没有 `CharacterBody3D`、没有状态机、没有关卡、没有输入映射。** 想做玩法得从零加。

## 10. 改这个项目的注意事项

- 改 `.gd` 后必须验证：脚本解析失败时 `godot --import` **仍返回 0**，
  `verify.ps1` 会额外解析输出里的 `Parse Error` / `SCRIPT ERROR` 才作数。
- 不要手工改 `.tscn` 里的 `uid://`（Godot 4 靠 uid 找资源，改错会「资源丢失」）。
  新场景让脚本生成或按现有 `Main.tscn` 的格式写。
- `project.godot` 被 **Godot 编辑器打开过一次就会被官方模板覆写**（丢注释和
  `[rendering]` 段）。发现被覆写要恢复。
- Blender 导入 GLB 后会多出一个在 `glTF_not_exported` 集合里的 42 顶点球，
  渲染/算包围盒时必须排除，否则预览相机会被撑大。
- 预览图放 `docs/preview/`，命名 `<资产名>.godot.png`（Godot 真实渲染）
  与 `assets/characters/<资产名>/<资产名>.preview.png`（Blender 侧）。
