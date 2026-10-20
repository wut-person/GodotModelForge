<#
.SYNOPSIS
  一键演示：参考图 → AI 图生3D → 重拓扑 → 自动绑定 → Godot 真实渲染截图。

.DESCRIPTION
  给录屏用的一条命令。每一步打印时间戳与耗时，结尾给汇总表。
  默认参数就是 README 里那组实测数据（约 67 秒生成、约 35 秒重拓扑与绑定）。

  产物：
    中间网格  assets\references\<name>_raw.glb
    落地资产  assets\characters\<name>\<name>.glb (+ meta.json / .import)
    截图      docs\preview\<name>.godot.png

.PARAMETER Image
  参考图路径。默认 assets\references\demo_official.png（官方 demo 图：有纹理、有自然光照）。
  实测：无贴图的灰模会被生成成一张扁片，录屏请务必用真实照片。

.PARAMETER Name
  落地资产名，默认 demo_ai（小写 + 下划线）。

.PARAMETER Steps
  扩散步数，默认 30。只想快速看效果时用 12。

.PARAMETER Octree
  八叉树分辨率，默认 256。调大更细但更慢更吃显存。

.PARAMETER TargetFaces
  重拓扑目标三角面数，默认 9000（游戏角色常用 5k–20k）。

.PARAMETER Preview
  额外渲染一张 Blender 预览图（带骨骼）到资产目录。

.PARAMETER NoScreenshot
  跳过最后的 Godot 截图（无显卡/无窗口环境用）。

.PARAMETER Cleanup
  结束后删掉中间网格 <name>_raw.glb。

.PARAMETER Pause
  结束时等待回车，双击运行时窗口不会一闪而过。

.EXAMPLE
  tools\demo.cmd

.EXAMPLE
  tools\demo.cmd -Steps 12 -TargetFaces 6000 -NoScreenshot

.NOTES
  本文件必须是 UTF-8 with BOM，否则 PowerShell 5.1 会按 ANSI 读，中文全乱。
#>
[CmdletBinding()]
param(
    [string]$Image = '',
    [string]$Name = 'demo_ai',
    [int]$Steps = 30,
    [int]$Octree = 256,
    [int]$TargetFaces = 9000,
    [switch]$Preview,
    [switch]$NoScreenshot,
    [switch]$Cleanup,
    [switch]$Pause
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

function Say([string]$msg) {
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $msg)
}
function Rule {
    Write-Host ('-' * 74) -ForegroundColor DarkGray
}

# ---------- 路径与环境 ----------
$PipeDir = $PSScriptRoot
$Root = Split-Path -Parent (Split-Path -Parent $PipeDir)
if (-not (Test-Path (Join-Path $Root 'project.godot'))) {
    throw "找不到 Godot 项目根：$Root"
}

$Py = $env:BLENDER_PY
if (-not $Py) { $Py = 'D:\Tools\Python313-blender\python.exe' }
$Godot = $env:GODOT_BIN
if (-not $Godot) { $Godot = 'D:\Tools\Godot\Godot_v4.7.2-stable_win64_console.exe' }

if (-not $Image) { $Image = Join-Path $Root 'assets\references\demo_official.png' }

foreach ($need in @($Py, $Godot, $Image)) {
    if (-not (Test-Path $need)) { throw "缺少依赖：$need" }
}

$rawGlb   = Join-Path $Root ('assets\references\' + $Name + '_raw.glb')
$assetRel = 'res://assets/characters/' + $Name + '/' + $Name + '.glb'
$shotRel  = 'docs/preview/' + $Name + '.godot.png'
$metaPath = Join-Path $Root ('assets\characters\' + $Name + '\' + $Name + '.meta.json')

$total = [Diagnostics.Stopwatch]::StartNew()
$timings = @()

Rule
Say 'AI 资产生产管线 · 一键演示'
Say ('参考图   : ' + $Image)
Say ('资产名   : ' + $Name)
Say ('参数     : steps=' + $Steps + '  octree=' + $Octree + '  targetFaces=' + $TargetFaces)
Rule

# ---------- 1/4 图生3D ----------
Say ('[1/4] AI 图生3D（Hunyuan3D-2mini · ' + $Steps + ' 步 · octree ' + $Octree + '）')
$sw = [Diagnostics.Stopwatch]::StartNew()
& $Py (Join-Path $Root 'tools\pipeline\hunyuan3d_shape.py') --image $Image --out $rawGlb --steps $Steps --octree $Octree
$code = $LASTEXITCODE
$sw.Stop()
if ($code -ne 0) { throw ('图生3D 失败，退出码 ' + $code) }
if (-not (Test-Path $rawGlb)) { throw '图生3D 没有产出 glb 文件' }
$rawKB = [math]::Round((Get-Item $rawGlb).Length / 1KB, 1)
$t1 = [math]::Round($sw.Elapsed.TotalSeconds, 1)
$timings += [pscustomobject]@{ 步骤 = '1. 图生3D'; 耗时秒 = $t1 }
Say ('      OK  输出 ' + $rawKB + ' KB，耗时 ' + $t1 + ' 秒')

# ---------- 2/4 重拓扑 + 绑定 + 自检 ----------
Say ('[2/4] 重拓扑 + 自动绑定 + Godot 自检（目标 ' + $TargetFaces + ' 面）')
$sw = [Diagnostics.Stopwatch]::StartNew()
$rigArgs = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', (Join-Path $Root 'tools\pipeline\rig_asset.ps1'),
    '-In', $rawGlb,
    '-Name', $Name,
    '-Prepare',
    '-TargetFaces', "$TargetFaces",
    '-Uv', 'smart'
)
if ($Preview) { $rigArgs += '-Preview' }
& powershell.exe @rigArgs
$code2 = $LASTEXITCODE
$sw.Stop()
$t2 = [math]::Round($sw.Elapsed.TotalSeconds, 1)
$timings += [pscustomobject]@{ 步骤 = '2. 重拓扑+绑定+自检'; 耗时秒 = $t2 }
if ($code2 -ne 0) { throw ('重拓扑/绑定/自检失败，退出码 ' + $code2) }
Say ('      OK  落地 ' + $assetRel + '，耗时 ' + $t2 + ' 秒')

# ---------- 3/4 Godot 截图 ----------
if ($NoScreenshot) {
    Say '[3/4] 已跳过 Godot 截图（-NoScreenshot）'
}
else {
    Say '[3/4] Godot 真实渲染截图（会短暂弹出窗口，这是正常的）'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $Godot --path $Root --script 'res://tools/godot/screenshot.gd' -- ('--screenshot=' + $shotRel) ('--asset=' + $assetRel)
    $code3 = $LASTEXITCODE
    $sw.Stop()
    $t3 = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $timings += [pscustomobject]@{ 步骤 = '3. Godot 截图'; 耗时秒 = $t3 }
    if ($code3 -ne 0) {
        Say ('      !  截图退出码 ' + $code3 + '，检查 docs\preview 下是否有图')
    }
    else {
        Say ('      OK  截图 ' + $shotRel + '，耗时 ' + $t3 + ' 秒')
    }
}

# ---------- 4/4 汇总 ----------
Say '[4/4] 汇总'
$total.Stop()
$sec = [math]::Round($total.Elapsed.TotalSeconds, 1)

if (Test-Path $metaPath) {
    try {
        $meta = Get-Content -LiteralPath $metaPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $clips = ''
        if ($meta.clips) { $clips = ($meta.clips -join ' / ') }
        $inTri = ''
        if ($meta.prepare -and $meta.prepare.input) { $inTri = $meta.prepare.input.triangles }
        Rule
        Say ('资产      : ' + $Name + '.glb')
        Say ('面数      : ' + $meta.triangles + ' 三角面（输入 ' + $inTri + ' 面）')
        Say ('顶点      : ' + $meta.vertices)
        Say ('骨骼      : ' + $meta.bone_count + ' 根')
        Say ('动画      : ' + $clips)
        Say ('未绑定顶点: ' + $meta.unweighted_vertices + '（必须为 0）')
        Say ('身高      : ' + [math]::Round($meta.height_m, 3) + ' m')
    }
    catch {
        Say ('meta.json 解析失败：' + $_.Exception.Message)
    }
}

Rule
$timings | Format-Table -AutoSize | Out-String | Write-Host
Say ('总耗时 ' + $sec + ' 秒')
if ($NoScreenshot) {
    Say '完成（未截图）'
}
else {
    Say ('完成，截图在 ' + (Join-Path $Root $shotRel))
}
Rule

if ($Cleanup -and (Test-Path $rawGlb)) {
    Remove-Item -LiteralPath $rawGlb -Force -ErrorAction SilentlyContinue
    Say '已删除中间网格 ' + $rawGlb
}

if ($Pause) {
    Write-Host ''
    Read-Host '按回车退出' | Out-Null
}
