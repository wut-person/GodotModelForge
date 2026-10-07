<#
.SYNOPSIS
  生成一个带骨骼与动画的 3D 角色 GLB，并落到 Godot 项目里。

.DESCRIPTION
  两种执行后端，自动探测（也可用环境变量强制指定）：

    1) bpy 模块（推荐，无需安装 Blender）
       D:\Tools\Python313-blender\python.exe 里装了 bpy（Blender 作为 Python 库）
       设置 BLENDER_PY 指向该解释器即可覆盖。

    2) Blender 可执行文件
       设置 BLENDER_BIN 指向 blender.exe。

  生成结束后可选自动跑一次 Godot 验证（-Verify）或渲染预览图（-Preview）。

.PARAMETER Name
  资产名，决定输出目录与文件名，例如 hero / slime / boss01

.PARAMETER Height
  角色总高（米），默认 1.8

.PARAMETER Seed
  随机种子（当前程序化模型仅记录，供后期体型随机化使用）

.PARAMETER NoAnim
  不生成动画，只出静态绑定模型

.PARAMETER Force
  目标文件已存在时也重新生成

.PARAMETER Verify
  生成后立刻调用 tools\verify.ps1 做 Godot 侧导入与自检

.PARAMETER Preview
  生成后渲染一张 PNG 预览图（Blender Workbench 引擎，无需 GPU 光追）

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\pipeline\build_character.ps1 -Name hero -Force -Verify
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Name,
  [double]$Height = 1.8,
  [int]$Seed = 0,
  [switch]$NoAnim,
  [switch]$Force,
  [switch]$Verify,
  [switch]$Preview
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$GenScript   = (Resolve-Path (Join-Path $PSScriptRoot '..\blender\gen_character.py')).Path
$OutDir      = Join-Path $ProjectRoot "assets\characters\$Name"

# ---------------------------------------------------------------- 选择执行后端
$BlenderPy  = if ($env:BLENDER_PY)  { $env:BLENDER_PY }  else { 'D:\Tools\Python313-blender\python.exe' }
$BlenderBin = if ($env:BLENDER_BIN) { $env:BLENDER_BIN } else { 'D:\Tools\blender-4.5.3-windows-x64\blender.exe' }

$Mode = $null
$bpyVersion = $null
if (Test-Path $BlenderPy) {
  $bpyVersion = & $BlenderPy -c "import bpy; print(bpy.app.version_string)" 2>$null
  if ($LASTEXITCODE -eq 0 -and $bpyVersion) { $Mode = 'module' }
}
if (-not $Mode -and (Test-Path $BlenderBin)) { $Mode = 'binary' }

if (-not $Mode) {
  Write-Host "找不到可用的 Blender 后端。" -ForegroundColor Red
  Write-Host "  方式一（推荐）: 用 BLENDER_PY 指定装了 bpy 的 python.exe" -ForegroundColor Yellow
  Write-Host "  方式二        : 用 BLENDER_BIN 指定 blender.exe" -ForegroundColor Yellow
  Write-Host "  安装命令      : python3.13 -m pip install bpy" -ForegroundColor DarkGray
  exit 2
}

$target = Join-Path $OutDir "$Name.glb"
$skipGen = (Test-Path $target) -and (-not $Force)
if ($skipGen) {
  Write-Host "已存在，跳过生成: $target" -ForegroundColor Yellow
  Write-Host "（需要重新生成请加 -Force）" -ForegroundColor DarkGray
} else {
  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
  $genArgs = @('--name', $Name, '--out', $OutDir, '--seed', "$Seed", '--height', "$Height")
  if ($NoAnim) { $genArgs += '--no-anim' }

  if ($Mode -eq 'module') {
    Write-Host "后端: bpy 模块 ($BlenderPy)" -ForegroundColor Cyan
    Write-Host "     Blender 版本: $bpyVersion" -ForegroundColor DarkGray
    $cmd  = $BlenderPy
    $call = @($GenScript) + $genArgs
  } else {
    Write-Host "后端: Blender 可执行文件 ($BlenderBin)" -ForegroundColor Cyan
    $cmd  = $BlenderBin
    $call = @('--background', '--factory-startup', '--python', $GenScript, '--') + $genArgs
  }

  Write-Host "输出目录: $OutDir" -ForegroundColor Cyan
  Write-Host "开始生成……" -ForegroundColor Cyan

  $env:PYTHONIOENCODING = 'utf-8'
  # PowerShell 5.1 会把原生命令写到 stderr 的每一行都当成 NativeCommandError，
  # 配合 $ErrorActionPreference='Stop' 会直接中断整个脚本（而 Blender 习惯把正常日志写 stderr）。
  # 因此调用外部程序期间临时降级为 Continue，只靠退出码判断成败。
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $output = & $cmd @call 2>&1 | Out-String
  $code = $LASTEXITCODE
  $ErrorActionPreference = $prevEAP

  $output -split "`r?`n" | Where-Object { $_ -match '^\[pipeline\]' } | ForEach-Object { Write-Host "    $_" }

  if ($code -ne 0 -or -not (Test-Path $target)) {
    Write-Host "生成失败（backend exit=$code）" -ForegroundColor Red
    $output -split "`r?`n" | Where-Object { $_ -match 'Error|Traceback|error:' } | Select-Object -Last 20 |
      ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
    exit 1
  }

  $sizeKb = [math]::Round((Get-Item $target).Length / 1KB, 1)
  Write-Host "生成成功: $target ($sizeKb KB)" -ForegroundColor Green

  $meta = Join-Path $OutDir "$Name.meta.json"
  if (Test-Path $meta) {
    $m = Get-Content $meta -Raw | ConvertFrom-Json
    Write-Host ("  顶点={0}  三角面={1}  骨骼={2}  动画片段={3}" -f `
      $m.vertices, $m.triangles, $m.bones.Count, ($m.clips -join '/')) -ForegroundColor DarkGray
  }
}

# ---------------------------------------------------------------- 可选：预览图
if ($Preview) {
  $PreviewScript = (Resolve-Path (Join-Path $PSScriptRoot '..\blender\render_preview.py')).Path
  $png = Join-Path $OutDir "$Name.preview.png"
  Write-Host "渲染预览图 -> $png" -ForegroundColor Cyan
  $prevArgs = @('--glb', $target, '--out', $png, '--width', '640', '--height', '640')
  $env:PYTHONIOENCODING = 'utf-8'
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  if ($Mode -eq 'module') {
    & $BlenderPy $PreviewScript @prevArgs 2>&1 |
      Where-Object { $_ -match '^\[preview\]' } | ForEach-Object { Write-Host "    $_" }
  } else {
    & $BlenderBin --background --factory-startup --python $PreviewScript -- @prevArgs 2>&1 |
      Where-Object { $_ -match '^\[preview\]' } | ForEach-Object { Write-Host "    $_" }
  }
  $ErrorActionPreference = $prevEAP
  if (Test-Path $png) { Write-Host "预览图已生成: $png" -ForegroundColor Green }
  else { Write-Host "预览图渲染失败（不影响模型本身）" -ForegroundColor Yellow }
}

# ---------------------------------------------------------------- 可选：自动验证
if ($Verify) {
  Write-Host "`n调用 Godot 验证……" -ForegroundColor Cyan
  $vArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $ProjectRoot 'tools\verify.ps1'),
             '-Asset', "res://assets/characters/$Name/$Name.glb")
  & powershell.exe @vArgs
  exit $LASTEXITCODE
}

exit 0
