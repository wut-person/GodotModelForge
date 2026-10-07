# 3D 角色资产规范（Godot 落地标准）

本文件定义「生成出来的 3D 角色」必须满足什么条件才算合格。
`tools/verify.ps1` 会按这份规范自动检查，不合格直接报错退出。

---

## 1. 目录与命名

```
res://assets/characters/<资产名>/          ← 一个角色一个目录，目录名 == 资产名
├── <资产名>.glb                            ← 主资产（唯一必需文件）
├── <资产名>.glb.import                     ← Godot 自动生成，不要手工编辑
├── <资产名>.meta.json                      ← 生成元数据（来源、骨骼、片段清单）
├── <资产名>_albedo.png                     ← 贴图（可选，命名固定后缀）
├── <资产名>_normal.png
└── <资产名>_orm.png                        ← R=AO G=Roughness B=Metallic
```

命名规则：

| 规则 | 说明 |
|---|---|
| 全小写 + 下划线 | `hero_armored.glb` ✅ / `HeroArmored.glb` ❌ |
| 不要中文、空格 | Godot 导出到某些平台时中文路径会出问题 |
| 一个角色一个目录 | 贴图/变体跟着角色走，避免 `assets/textures` 里堆几百个文件 |
| 版本变体用后缀 | `hero_v2.glb`，不要 `hero_new_final.glb` |

## 2. 单位、朝向、原点

| 项 | 规定 | 原因 |
|---|---|---|
| 单位 | 1 单位 = 1 米 | Godot 的物理/光照参数都按米调 |
| 身高 | 人形 1.6–2.0 m，怪物按设定但要有明确记录 | 相机与碰撞体尺寸依赖它 |
| 朝向 | **+Z**（Godot 里角色面朝 +Z） | Godot 的 `Node3D.look_at()`、`CharacterBody3D` 惯例 |
| 原点 | 双脚之间的地面点 =(0,0,0) | 便于直接放到地形上，不用再对 Y 偏移 |
| 上轴 | +Y | Godot 默认 |

> Blender 是 Z-up、正面朝 -Y。导出 glTF 时勾选 `export_yup=True`，
> 并在 Blender 里让角色面朝 **-Y**，导出到 Godot 后即为面朝 +Z。

## 3. 骨架规范

人形统一使用下面这套骨骼名。**唯一的权威定义在 `tools/blender/rig_lib.py`**，
`gen_character.py`（程序化生成）和 `auto_rig.py`（自动绑定）都从那里取，
不允许各自复制一份。后期接 Mixamo / UniRig / Rigify 的产物时，按这张表做名字映射即可复用动画库。

| 骨骼 | 父级 | 说明 |
|---|---|---|
| `hips` | — | 根骨骼，承载位移与整体上下起伏 |
| `spine` | hips | 腰椎 |
| `chest` | spine | 胸椎，手臂挂点 |
| `neck` | chest | |
| `head` | neck | |
| `shoulder.L/R` | chest | 锁骨，容易被忽略但决定抬手自然度 |
| `upperarm.L/R` | shoulder | |
| `forearm.L/R` | upperarm | |
| `hand.L/R` | forearm | |
| `thigh.L/R` | hips | |
| `shin.L/R` | thigh | |
| `foot.L/R` | shin | 脚尖朝前 |

硬性要求：

- 骨骼总数 **≥ 1**，人形建议 15–70 根（超过 100 根对移动端不友好）
- 所有影响网格的骨骼必须真的被蒙皮引用（`verify.ps1` 会查 `skin`）
- **不允许有未绑定权重的顶点**：会出现「顶点塌到原点」的撕裂现象
  （`gen_character.py` 会统计并打印未绑定顶点数，必须为 0）
- 骨架根节点必须是 `Skeleton3D` 的祖先，且变换为**单位变换**（不要带缩放）

## 4. 动画片段规范

- 每个动作一个**独立命名**的动画片段，导入后在 Godot 里表现为 `AnimationPlayer` 的多个 animation
- 命名用大驼峰、语义清晰：`Idle` / `Walk` / `Run` / `Attack` / `Hit` / `Death`
- 循环动作（Idle/Walk/Run）**首尾关键帧必须一致**，否则循环时会有跳变
- 至少 1 个片段；`verify.ps1` 默认要求 `--min-clips 1`，可按资产提高

导入路径（Godot 4）：选中 `.glb` → Import 面板 → `Animation > Import` 保持默认，
导入后动画会以 `<资产名>/<片段名>` 或直接片段名的形式出现在 `AnimationPlayer` 里。

## 5. 网格与材质预算

| 项 | 建议上限 | 备注 |
|---|---|---|
| 三角面 | 主角 5 万 / 小怪 1 万 / 移动端 5 千 | 图生3D 出来的原始网格通常 10 万+，必须减面 |
| 材质槽 | ≤ 4 个 | 每个材质槽 = 一次 draw call |
| 贴图 | 2048² 以内，主角 1024²–2048²，小怪 512²–1024² | 用 ORM 打包，不要三张独立灰度图 |
| 骨骼影响 | 每顶点 ≤ 4 根 | Godot/移动端 GPU 蒙皮常见上限 |
| 贴图格式 | `.png`（无损）或 `.jpg`（无 alpha） | 导入后 Godot 会自动压成 VRAM 格式 |

## 6. 生成管线契约

无论模型是「程序化生成」「图生3D」还是「文生3D」，最终都必须产出上面这套结构，
并且必须通过验证。**静态网格进来之后，统一由自动绑定补齐骨骼与动画。**

### 6.1 两条入口

```powershell
# A) 程序化生成（自带网格 + 骨骼 + 动画）
pwsh -File tools\pipeline\build_character.ps1 -Name hero -Force -Verify -Preview

# B) 任意静态网格 → 自动套骨架 + 动画（图生3D / 文生3D / 下载的模型走这条）
pwsh -File tools\pipeline\rig_asset.ps1 -In D:\ai_out\monster.glb -Preview -Screenshot
```

### 6.2 自动绑定对输入网格的要求

`auto_rig.py` 靠几何特征推断体型，对输入有以下期望（不满足时会退化到人体比例兜底，
但效果会变差）：

| 输入条件 | 为什么 |
|---|---|
| 网格**竖直站立**、脚底大致水平 | 高度方向切片分析的前提；躺着的模型会把「身高」算成「身长」 |
| 身高方向 = +Z（Blender 坐标） | 若模型是 Y-up 的原始数据，先旋转再绑定 |
| 人形或类人形（有头、躯干、两条腿、两条手臂） | 骨架模板是人形 19 骨骼 |
| 左右大致对称 | 手臂/腿的检测依赖左右对称性 |
| 单一网格（多块会自动合并） | 合并后才能算出一致的骨架 |

自动绑定能处理的「不标准」情况：

- 手臂贴身下垂（靠 X/Y 跨度差异区分手臂与躯干，不依赖「手臂张开」）
- 大头短腿、长臂等夸张比例（用实测的髋宽/肩高/臂长覆盖经验比例）
- 脚部并拢（`legs_split=False` 时按髋宽摆放腿骨）
- 没有手臂的怪物（会打印警告并按人体比例放手臂骨，此时建议 `-Clips none` 或自备骨架）

### 6.3 验证

```powershell
# 一键验证：脚本解析 → Godot 导入 → 资产自检
pwsh -File tools\verify.ps1 -Asset res://assets/characters/hero/hero.glb -MinBones 15 -MinClips 3
```

验证不通过就不算「生成成功」。**不要**在验证失败的情况下继续往上叠场景和玩法。

### 6.4 从现成模型造测试样本

```powershell
python tools\pipeline\strip_rig.py --in hero.glb --out hero_static.glb
```

剥掉骨骼/蒙皮/动画，得到与「图生3D 产出」等价的静态网格，
用来回归验证自动绑定链路是否仍然可用。

## 7. 已知坑

1. **新增 `.glb` 后 Godot 不会自动看到** —— 必须跑一次 `godot --headless --import`，
   否则 `load()` 返回 null。`verify.ps1` 已经内置这一步。
2. **`.glb.import` 文件要提交到版本库**，但 `.godot/` 目录不要提交（它是缓存）。
3. **不要手工改 `.tscn` 里的 `uid://`**，Godot 4 靠 uid 找资源，改错会变成「资源丢失」。
   需要新场景时用 `tools` 里的脚本或让 DSH 生成，不要凭记忆手写 uid。
4. **Blender 导出时忘记勾 `export_apply`** → 细分/镜像修改器不生效，网格和预览不一致。
5. **图生3D 的网格是三角汤、没有 UV** → 必须走「重拓扑 + Smart UV Project + 烘焙」才能进游戏，
   直接用会得到拉伸的贴图。
