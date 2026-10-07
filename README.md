# GodotModelForge

**AI 生成 3D 模型 → 自动导入 Godot** 的落地工程。

这个项目解决的核心问题不是「怎么生成模型」，而是
**「生成出来的东西怎么可靠地变成 Godot 里能用的资产，并且能被自动验证」**。
生成端（程序化建模 / 图生3D / 文生3D）可以随便换，但落地标准和验证必须稳定。

---

## 当前状态

| 环节 | 状态 | 说明 |
|---|---|---|
| Godot 引擎接入 | ✅ 已完成 | Godot **4.7.2** stable，可无头读写项目、跑导入、跑自检 |
| 项目骨架 | ✅ 已完成 | 目录规范、`.tscn`/`.gd` 全部可被 DSH 直接读写 |
| 自动验证 | ✅ 已完成 | 脚本解析 + 资源导入 + 资产自检 三关，返回退出码 |
| 建模后端 | ✅ 已完成 | **Blender 5.2.2**（以 `bpy` Python 模块方式运行，无需安装 Blender） |
| 端到端链路 | ✅ 已跑通 | 程序化生成带骨骼动画的角色 → GLB → Godot 导入 → 自检全绿 |
| 自动绑定（auto-rig） | ✅ 已完成 | **任意静态网格 → 分析体型 → 套 19 骨骼人形骨架 → 蒙皮 → 套 4 段动画 → Godot 验证通过** |
| AI 图生3D 生成 | ⏳ 待接入 | 链路已就绪（静态网格进来即可自动绑定），等接生成模型 |

### 跑通的样子

Blender 侧模型（Workbench 渲染，带骨骼显示）：

![hero 模型预览](assets/characters/hero/hero.preview.png)

Godot 侧真实渲染（Vulkan / Forward+，自动截图）：

![Godot 场景截图](docs/preview/hero.godot.png)

自动绑定链路产出的资产（静态网格 → 自动套骨架，同样在 Godot 里渲染）：

![自动绑定结果](docs/preview/autorig_full.godot.png)

---

## 环境（本机已装好）

| 组件 | 路径 | 说明 |
|---|---|---|
| Godot 4.7.2 | `D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe` | 用 `_console.exe` 版本才能拿到控制台输出 |
| Blender 5.2.2 (bpy) | `D:\Tools\Python313-blender\python.exe` | 专用 Python 3.13 + `bpy` 模块 |
| 项目本体 | `D:\deepseek Flash\GodotModelForge` | 本目录 |

环境变量覆盖（可选）：

```powershell
$env:GODOT_BIN   = 'D:\path\to\godot_console.exe'   # verify.ps1 用
$env:BLENDER_PY  = 'D:\path\to\python.exe'          # 装了 bpy 的解释器
$env:BLENDER_BIN = 'D:\path\to\blender.exe'         # 有完整 Blender 时用这个
```

> ⚠️ 本机 PowerShell 执行策略禁止直接运行 `.ps1`。
> 请用 `tools\*.cmd`（内部已带 `-ExecutionPolicy Bypass`），
> 或手动 `powershell -ExecutionPolicy Bypass -File <脚本>`。

---

## 目录结构

```
GodotModelForge/
├── project.godot                 Godot 项目定义
├── icon.svg
├── assets/                       ← 生成产物落地处
│   ├── characters/<资产名>/       ← 一个角色一个目录
│   │   ├── <资产名>.glb           主资产
│   │   ├── <资产名>.meta.json     生成元数据（来源/骨骼/片段清单）
│   │   └── <资产名>.preview.png   预览图
│   ├── props/  environments/  textures/
│   └── references/               参考图与中间产物（.gdignore，Godot 不扫描）
├── scenes/Main.tscn              观察窗口 + 无头自检入口
├── scripts/
│   ├── Main.gd                   命令行参数解析 / 自检 / 展示
│   └── AssetInspector.gd         资产规范检查器（网格/骨骼/蒙皮/动画/材质）
├── tools/
│   ├── build.cmd                 一键：程序化生成角色 + 验证（+ 预览图 / 截图）
│   ├── rig.cmd                   一键：给任意静态网格自动套骨架加动画 + 验证
│   ├── verify.cmd                一键：验证
│   ├── verify.ps1                三关校验实现
│   ├── blender/
│   │   ├── rig_lib.py            ★ 共享库：骨骼规范 / 蒙皮权重算法 / 动画库 / GLB 导出
│   │   ├── gen_character.py      程序化建模（自带网格，走共享库绑定）
│   │   ├── auto_rig.py           ★ 自动绑定：分析体型 → 贴合骨架 → 蒙皮 → 加动画
│   │   └── render_preview.py     渲染预览图（Workbench，无需 GPU 光追）
│   ├── pipeline/
│   │   ├── build_character.ps1   程序化生成的入口
│   │   ├── rig_asset.ps1         自动绑定的入口
│   │   └── strip_rig.py          剥离 GLB 的骨骼/动画（造测试样本）
│   ├── godot/
│   │   └── screenshot.gd         Godot 真实渲染截图（SceneTree 脚本，支持 --asset 指定角色）
│   └── _fixtures/                测试样本（已 gitignore，可重新生成）
└── docs/ASSET_SPEC.md            资产规范（命名/朝向/骨骼/预算/坑）
```

---

## 怎么用

### 给任意模型自动套骨架并加动画（核心能力）

图生3D / 文生3D / 网上下载的模型，通常只有**一个静态网格**：没骨骼、没蒙皮、没动画。
这个命令把它变成可直接进游戏的动画角色：

```cmd
tools\rig.cmd "D:\ai_out\monster.glb" -Preview -Screenshot
```

依次执行：分析体型（总高/髋宽/肩宽/手臂伸展/腿是否分开）→ 贴合 19 骨骼标准人形骨架
→ 计算蒙皮权重（带左右串扰惩罚）→ 套用 Idle/Walk/Run/Attack 四段动画
→ 导出 GLB → Godot 导入并自检 → 渲染预览图与场景截图。

常用参数：

| 参数 | 作用 |
|---|---|
| `-Name myasset` | 指定落地资产名（默认取输入文件名） |
| `-Height 2.4` | 缩放到目标身高（默认 0 = 保持原尺寸不缩放） |
| `-Clips Idle,Walk` | 只套指定动画；`-Clips none` = 只要骨骼不要动画 |
| `-SidePenalty 4` | 出现「左臂动右臂跟着动」时调大，抑制左右串扰 |
| `-NoVerify` | 跳过 Godot 验证（不推荐） |

### 剥离现成模型的骨骼/动画（造测试样本、清瘦资产）

```cmd
python tools\pipeline\strip_rig.py --in hero.glb --out hero_static.glb --report strip.json
```

只保留网格与材质，去掉骨架/蒙皮/动画。上面的自动绑定链路就是用它造出
「图生3D 风格」的输入来验证的（129 KB → 16.8 KB）。

### 生成一个新角色并验证

```cmd
tools\build.cmd myhero -Force -Preview -Screenshot
```

依次执行：Blender 建模导出 GLB → 渲染 Blender 预览图 → **调 Godot 导入并自检**
→ 用 Godot 真实渲染截一张场景图。全绿才算成功。

只想要其中几步就去掉对应开关（`-Preview` / `-Screenshot`），验证是默认开启的。

### 只验证已有资产

```cmd
tools\verify.cmd res://assets/characters/hero/hero.glb 15 3
```

参数：资产路径、最少骨骼数、最少动画片段数。

### 只截 Godot 场景图

```cmd
D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe --path . --script res://tools/godot/screenshot.gd -- --screenshot=docs/preview/hero.godot.png
```

注意：截图必须用**真实渲染驱动**，`--headless` 下拿不到画面。

### 在 Godot 里肉眼看

```cmd
D:\Tools\Godot\Godot_v4.7.2-stable_win64.exe --path "D:\deepseek Flash\GodotModelForge"
```

`Main.tscn` 会自动加载 `hero.glb`、播放第一个动画并让角色缓慢旋转。

### DSH/AI 自动化用的无头自检

```cmd
D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe --headless --path . -- --check=res://assets/characters/hero/hero.glb --json --min-bones 15 --min-clips 3
```

输出一行 JSON，退出码 0=通过 / 1=失败，便于脚本判断。

---

## 为什么这样设计

1. **验证优先于生成。** 生成模型很容易「看起来生成了」，但导入 Godot 后可能是空网格、
   丢骨骼、丢动画。所以第一件事是把 `verify.ps1` 建起来，之后所有生成器都必须过这一关。
2. **生成端可插拔。** `gen_character.py` 现在用的是程序化建模（够验证链路，但不好看）。
   接图生3D 时替换的是「网格来源」，而骨架、权重、导出、验证这四步完全复用。
3. **规范写进代码而不只是文档。** 骨骼命名、朝向、单位、未绑定顶点数都写进了
   `gen_character.py` 与 `AssetInspector.gd`，不靠人记 `docs/ASSET_SPEC.md`。
4. **不用完整 Blender 也能跑。** `bpy` 模块方式避免了 Blender 官网在国内 403、
   镜像站要过 JS 校验的问题，`pip install bpy` 一条命令搞定，也更容易被自动化复现。
5. **自动绑定是「生成」和「能用」之间的桥。** 生成模型这一步再强，产出的也只是静态网格。
   真正让资产可用的是 rigging：骨架、蒙皮、动画。所以这部分做成了独立、可复用、
   不关心网格来源的能力——换任何生成器，这一个环节都不用重做。

---

## 下一步（按优先级）

1. **接入图生3D**：单张参考图 → 网格 → 直接喂给 `tools\rig.cmd`（自动绑定已就绪）。
   6 GB 显存是硬约束，需要量化模型或分阶段（先生成网格，再单独跑贴图）。
2. **重拓扑 + UV + 烘焙**：图生3D 的输出通常是三角汤、无 UV，贴图会拉伸，必须处理。
3. **怪物/四足骨架**：现在自动绑定的骨架是人形 19 骨骼。非人形（四足、多足、飞行）
   需要另一套骨架模板 + 对应的体型分析规则。
4. **动画库扩充**：把 Mixamo / 免费动作库的片段按 `docs/ASSET_SPEC.md` 的骨骼命名映射进来，
   让 `-Clips` 能选到几十个动作。
5. **Godot 侧集成**：把验证通过的角色做成可复用的 `CharacterBody3D` 场景模板（含状态机）。

---

## 已知坑（都踩过一遍了）

| 坑 | 现象 | 解决 |
|---|---|---|
| `.ps1` 无 BOM | PS 5.1 按 ANSI 读，中文字符串被截断，报「字符串缺少终止符」 | `.ps1` 必须存成 **UTF-8 with BOM**（用 edit 工具改过后容易丢，改完要补） |
| `.cmd` 里有中文 | cmd.exe 按 ANSI 读，中文注释被当成命令执行，报「不是内部或外部命令」 | `.cmd` **只写 ASCII**，中文说明放 `.ps1` 的注释里 |
| Blender 把日志写 stderr | PS 5.1 + `$ErrorActionPreference='Stop'` 直接把脚本打断 | 调外部程序时临时降级为 `Continue`，只认退出码 |
| `godot --import` 退出码不可靠 | 脚本解析失败它照样返回 0 | 必须额外解析输出里的 `Parse Error` / `SCRIPT ERROR` |
| `.glb` 不导入就 `load()` 不到 | 返回 null，容易误判成「生成失败」 | 新增资产后必须跑一次 `godot --headless --import` |
| glTF 导入器的 icosphere | Blender 导入 GLB 后多出一个 42 顶点球，把预览相机的包围盒撑大 | 它在 `glTF_not_exported` 集合里，渲染/算包围盒时要排除 |
| Blender 5.x 的 Action 结构 | `action.fcurves` 不存在了（改成 layers/strips/channelbags） | `action_fcurves()` 兼容两种结构 |
| PowerShell 5.1 无 `pwsh` | 本机只有 Windows PowerShell 5.1 | 脚本不要用 PS7 专有语法，用 `powershell.exe` 调用 |
| `--headless` 截不到图 | headless 用 dummy 渲染驱动，viewport 没有画面 | 截图必须用真实渲染驱动（不加 `--headless`） |
| Godot CLI 脚本基类 | `--script` 的脚本继承 `Node` 会报 `Can't load the script` | 必须继承 `SceneTree` 或 `MainLoop` |
| Blender 官网 403 | `download.blender.org` 直连被拒，国内镜像要过 JS 校验 | 改用 `pip install bpy` 拿 Blender 作为 Python 模块 |

---

## 环境是怎么装起来的（可复现）

```powershell
# 1) Godot：自包含，解压即用
curl.exe -L -o godot.zip "https://downloads.godotengine.org/?version=4.7.2&flavor=stable&slug=win64.exe.zip&platform=windows.64"
Expand-Archive godot.zip -DestinationPath D:\Tools\Godot

# 2) Python 3.13（bpy 只提供 cp311 / cp313 轮子，本机原有的 3.12 用不了）
curl.exe -L -o py313.zip "https://www.python.org/ftp/python/3.13.7/python-3.13.7-embed-amd64.zip"
Expand-Archive py313.zip -DestinationPath D:\Tools\Python313-blender
# 编辑 python313._pth，取消 `import site` 的注释，然后装 pip
curl.exe -L -o get-pip.py https://bootstrap.pypa.io/get-pip.py
D:\Tools\Python313-blender\python.exe get-pip.py

# 3) Blender 作为 Python 模块（约 340 MB）
D:\Tools\Python313-blender\python.exe -m pip install bpy
```

