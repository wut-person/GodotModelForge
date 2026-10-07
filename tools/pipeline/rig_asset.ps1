<#
.SYNOPSIS
  给任意来源的静态网格（图生3D / 文生3D / 下载的模型）自动套人形骨架并加动画，落地到 Godot。

.DESCRIPTION
  这是「生成 → 可用资产」的最后一道工序：
    AI 生成的模型只有网格，没有骨骼、没有蒙皮、没有动画。
    本脚本负责分析体型 → 贴合标准人形骨架 → 算蒙皮权重 → 套动画库 → 导出 GLB
    → 可选渲染预览图 → 可选自动导入 Godot 并自检。

  输入支持 .glb / .gltf / .obj / .fbx。

.PARAMETER In
  输入模型路径（必填）

.PARAMETER Name
  落地资产名，默认取输入文件名（不含扩展名），会小写化并去掉非法字符

.PARAMETER Height
  目标身高（米）。默认 0 = 保持模型自身高度不缩放

.PARAMETER Clips
  要套用的动画片段，逗号分隔，默认 "Idle,Walk,Run,Attack"；填 none 表示只要骨骼不要动画

.PARAMETER SidePenalty
  左右串扰惩罚系数，默认 2.5。出现「左臂动、右臂跟着动」时可调大

.PARAMETER Preview
  生成后渲染一张 Blender 预览图

.PARAMETER Screenshot
  生成后用 Godot 真实渲染截一张场景图

.PARAMETER NoVerify
  跳过 Godot 导入与自检（不推荐）

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\pipeline\rig_asset.ps1 -In D:\ai_out\monster.glb -Preview -Screenshot
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$In,
  [string]$Name = '',
  [double]$Height = 0,
  [string]$Clips = 'Idle,Walk,Run,Attack',
  [double]$SidePenalty = 2.5,
  [switch]$Preview,
  [switch]$Screenshot,
  [switch]$NoVerify
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$AutoRig     = (Resolve-Path (Join-Path $PSScriptRoot '..\blender\auto_rig.py')).Path
$BlenderPy   = if ($env:BLENDER_PY) { $env:BLENDER_PY } else { 'D:\Tools\Python313-blender\python.exe' }

if (-not (Test-Path $BlenderPy)) {
  Write-Host "找不到带 bpy 的 Python: $BlenderPy" -ForegroundColor Red
  Write-Host "用 BLENDER_PY 指定，或先跑: python3.13 -m pip install bpy" -ForegroundColor Yellow
  exit 2
}

$srcPath = (Resolve-Path -LiteralPath $In).Path

# 资产名：默认取文件名，转小写并清掉 Godot 不喜欢的字符
if ([string]::IsNullOrWhiteSpace($Name)) {
  $Name = [System.IO.Path]::GetFileNameWithoutExtension($srcPath).ToLower()
}
$Name = ($Name -replace '[^a-zA-Z0-9_\-]', '_')

$OutDir = Join-Path $ProjectRoot "assets\characters\$Name"
$target = Join-Path $OutDir "$Name.glb"
$report = Join-Path $OutDir "$Name.autorig.json"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "输入  : $srcPath" -ForegroundColor Cyan
Write-Host "资产名: $Name" -ForegroundColor Cyan
Write-Host "输出  : $target" -ForegroundColor Cyan

# ---------------------------------------------------------------- 自动绑定
$rigArgs = @($AutoRig, '--in', $srcPath, '--out', $target,
             '--height', "$Height", '--clips', $Clips,
             '--side-penalty', "$SidePenalty", '--report', $report)
$env:PYTHONIOENCODING = 'utf-8'
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$output = & $BlenderPy @rigArgs 2>&1 | Out-String
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

$output -split "`r?`n" | Where-Object { $_ -match '^\[autorig\]' } | ForEach-Object { Write-Host "    $_" }

if ($code -ne 0 -or -not (Test-Path $target)) {
  Write-Host "自动绑定失败（exit=$code）" -ForegroundColor Red
  $output -split "`r?`n" | Where-Object { $_ -match 'Error|Traceback|error' } | Select-Object -Last 20 |
    ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
  exit 1
}

# 把分析报告也写成 meta.json，保持和 build_character.ps1 一致的目录结构
if (Test-Path $report) {
  Copy-Item $report (Join-Path $OutDir "$Name.meta.json") -Force
  $m = Get-Content $report -Raw | ConvertFrom-Json
  Write-Host ("  骨骼={0}  动画片段={1}  未绑定顶点={2}" -f `
    $m.bone_count, ($m.clips -join '/'), $m.unweighted_vertices) -ForegroundColor DarkGray
  if ($m.unweighted_vertices -gt 0) {
    Write-Host "  ⚠ 有未绑定顶点，Godot 里会撕裂，建议检查输入网格" -ForegroundColor Yellow
  }
} else {
  # 输入本身是 CmdletBinding 时不会生成报告，退化成不做统计
  Write-Host "  （未生成分析报告）" -ForegroundColor DarkGray
}

$sizeKb = [math]::Round((Get-Item $target).Length / 1KB, 1)
Write-Host "落地成功: $target ($sizeKb KB)" -ForegroundColor Green

# ---------------------------------------------------------------- 预览图
if ($Preview) {
  $PreviewScript = (Resolve-Path (Join-Path $PSScriptRoot '..\blender\render_preview.py')).Path
  $png = Join-Path $OutDir "$Name.preview.png"
  Write-Host "渲染预览图 -> $png" -ForegroundColor Cyan
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & $BlenderPy $PreviewScript --glb $target --out $png --width 640 --height 640 2>&1 |
    Where-Object { $_ -match '^\[preview\]' } | ForEach-Object { Write-Host "    $_" }
  $ErrorActionPreference = $prevEAP
}

# ---------------------------------------------------------------- Godot 验证
if (-not $NoVerify) {
  Write-Host "`n调用 Godot 验证……" -ForegroundColor Cyan
  $minClips = if ($Clips.Trim().ToLower() -eq 'none') { 0 } else { 1 }
  $vArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $ProjectRoot 'tools\verify.ps1'),
             '-Asset', "res://assets/characters/$Name/$Name.glb",
             '-MinBones', '15', '-MinClips', "$minClips")
  & powershell.exe @vArgs
  $verifyExit = $LASTEXITCODE
} else {
  $verifyExit = 0
}

# ---------------------------------------------------------------- Godot 截图
if ($Screenshot) {
  $Godot = if ($env:GODOT_BIN) { $env:GODOT_BIN } else { 'D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe' }
  if (Test-Path $Godot) {
    $shotRel = "docs/preview/$Name.godot.png"
    Write-Host "Godot 真实渲染截图 -> $shotRel" -ForegroundColor Cyan
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Godot --path $ProjectRoot --script "res://tools/godot/screenshot.gd" -- `
      "--screenshot=$shotRel" "--asset=res://assets/characters/$Name/$Name.glb" --frames=60 2>&1 |
      Where-Object { $_ -match '^\[screenshot\]' } | ForEach-Object { Write-Host "    $_" }
    $ErrorActionPreference = $prevEAP
  }
}

exit $verifyExit
