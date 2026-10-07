<#
.SYNOPSIS
  GodotModelForge 一键校验：脚本解析 → 资源导入 → 角色资产自检。

.DESCRIPTION
  由 DSH 在每次「生成/修改资产或脚本」之后调用，确保落地的东西真的能用。
  三个关卡任何一关失败都会以非零退出码结束。

.PARAMETER Asset
  要自检的角色资产路径（res:// 形式），默认 res://assets/characters/hero/hero.glb

.PARAMETER SkipScripts
  跳过 GDScript 逐文件解析检查。

.PARAMETER SkipImport
  跳过资源导入（谨慎使用；新增 .glb 后不导入 Godot 是看不到的）。

.PARAMETER MinBones
  自检要求的最少骨骼数，默认 1。

.PARAMETER MinClips
  自检要求的最少动画片段数，默认 1。

.EXAMPLE
  pwsh -File tools\verify.ps1
  pwsh -File tools\verify.ps1 -Asset res://assets/characters/slime/slime.glb -MinBones 0 -MinClips 2
#>
[CmdletBinding()]
param(
  [string]$Asset = "res://assets/characters/hero/hero.glb",
  [switch]$SkipScripts,
  [switch]$SkipImport,
  [int]$MinBones = 1,
  [int]$MinClips = 1
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Godot = if ($env:GODOT_BIN) { $env:GODOT_BIN } else { 'D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe' }

$script:Failures = @()

function Write-Stage([string]$n, [string]$title) { Write-Host "`n[$n] $title" -ForegroundColor Cyan }
function Write-Pass([string]$m) { Write-Host "    OK   $m" -ForegroundColor Green }
function Write-Fail([string]$m) { Write-Host "    FAIL $m" -ForegroundColor Red; $script:Failures += $m }
function Write-Info([string]$m) { Write-Host "    ..   $m" -ForegroundColor DarkGray }

# Godot 的错误既走 stdout 也走 stderr，统一捕获成字符串
function Invoke-Godot {
  param([string[]]$Arguments)
  $raw = & $Godot @Arguments 2>&1 | Out-String
  return $raw
}

if (-not (Test-Path $Godot)) {
  Write-Host "找不到 Godot 可执行文件: $Godot" -ForegroundColor Red
  Write-Host "设置环境变量 GODOT_BIN 指向 godot 控制台版 exe 即可。" -ForegroundColor Yellow
  exit 2
}

Write-Host "==============================================" -ForegroundColor White
Write-Host " GodotModelForge 校验" -ForegroundColor White
Write-Host " 项目: $ProjectRoot" -ForegroundColor White
Write-Host " 引擎: $(& $Godot --version 2>&1 | Select-Object -First 1)" -ForegroundColor White
Write-Host " 资产: $Asset" -ForegroundColor White
Write-Host "==============================================" -ForegroundColor White

# ---------------------------------------------------------------- 1) 脚本解析
if (-not $SkipScripts) {
  Write-Stage 1 'GDScript 解析检查'
  $gdFiles = Get-ChildItem -Path (Join-Path $ProjectRoot 'scripts') -Filter '*.gd' -Recurse -File -ErrorAction SilentlyContinue
  if ($gdFiles.Count -eq 0) { Write-Info '没有找到 .gd 文件' }
  foreach ($f in $gdFiles) {
    $rel = $f.FullName.Substring($ProjectRoot.Length + 1) -replace '\\', '/'
    $resPath = "res://$rel"
    $out = Invoke-Godot @('--headless', '--path', $ProjectRoot, '--check-only', '--script', $resPath)
    $bad = $out -split "`r?`n" | Where-Object { $_ -match 'Parse Error|SCRIPT ERROR|Failed to load script|^ERROR:' }
    if ($bad) {
      Write-Fail "$rel 解析失败"
      $bad | Select-Object -First 6 | ForEach-Object { Write-Host "         $($_.Trim())" -ForegroundColor DarkRed }
    } else {
      Write-Pass $rel
    }
  }
} else { Write-Stage 1 'GDScript 解析检查（已跳过）' }

# ---------------------------------------------------------------- 2) 资源导入
if (-not $SkipImport) {
  Write-Stage 2 '资源导入（godot --import）'
  $out = Invoke-Godot @('--headless', '--path', $ProjectRoot, '--import')
  # 导入阶段的脚本错误也要算失败：--import 的退出码不可靠
  $bad = $out -split "`r?`n" | Where-Object { $_ -match 'Parse Error|SCRIPT ERROR|Failed to load script|Failed to import|ERROR: Cannot open' }
  if ($bad) {
    Write-Fail '导入过程中报错'
    $bad | Select-Object -First 10 | ForEach-Object { Write-Host "         $($_.Trim())" -ForegroundColor DarkRed }
  } else {
    Write-Pass '导入完成，无错误'
  }

  # 确认目标资产在磁盘上确实存在（存在才谈得上被导入）
  $abs = Join-Path $ProjectRoot ($Asset -replace '^res://', '')
  if (Test-Path $abs) {
    $sidecar = "$abs.import"
    if (Test-Path $sidecar) { Write-Pass "已生成 .import 边车文件: $(Split-Path -Leaf $sidecar)" }
    else { Write-Info "尚无 .import 边车文件（首次导入可能仍在处理中）" }
  } else {
    Write-Info "磁盘上没有 $abs（跳过导入确认）"
  }
} else { Write-Stage 2 '资源导入（已跳过）' }

# ---------------------------------------------------------------- 3) 资产自检
Write-Stage 3 '角色资产自检（headless 运行）'
$args3 = @('--headless', '--path', $ProjectRoot, '--', "--check=$Asset", '--json', '--min-bones', "$MinBones", '--min-clips', "$MinClips")
$out = Invoke-Godot $args3
$code = $LASTEXITCODE

$jsonLine = $out -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
if ($jsonLine) {
  try {
    $r = $jsonLine | ConvertFrom-Json
    $i = $r.info
    Write-Info ("网格实例={0}  面数={1}  顶点数={2}" -f $i.mesh_instances, $i.surfaces, $i.vertices)
    Write-Info ("骨架={0}  骨骼数={1}  已蒙皮={2}" -f $i.skeletons, $i.bones, $i.has_skin)
    Write-Info ("动画片段={0}" -f ($i.clips -join ', '))
    Write-Info ("材质={0}" -f (($i.materials | Where-Object { $_ }) -join ', '))
    if ($r.ok) { Write-Pass '资产符合规范' }
    else {
      Write-Fail '资产不符合规范'
      $r.errors | ForEach-Object { Write-Host "         - $_" -ForegroundColor DarkRed }
    }
  } catch {
    Write-Fail "自检输出无法解析为 JSON: $($_.Exception.Message)"
    Write-Host $out -ForegroundColor DarkRed
  }
} else {
  Write-Fail "自检没有产出 JSON 结果（exit=$code）"
  ($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 25) | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkRed }
}

# ---------------------------------------------------------------- 汇总
Write-Host "`n==============================================" -ForegroundColor White
if ($Failures.Count -eq 0) {
  Write-Host " 全部通过 ✅" -ForegroundColor Green
  Write-Host "==============================================" -ForegroundColor White
  exit 0
} else {
  Write-Host " 失败 $($Failures.Count) 项 ❌" -ForegroundColor Red
  $Failures | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
  Write-Host "==============================================" -ForegroundColor White
  exit 1
}
