@echo off
REM One-click auto-rig: give any static mesh a humanoid skeleton plus animations, then verify in Godot.
REM Usage: tools\rig.cmd "<input.glb>" [-Name myasset] [-Preview] [-Screenshot] [-Height 1.8]
REM Note: keep this file ASCII-only. cmd.exe reads .cmd as ANSI and would mangle CJK text.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "SRC=%~1"
if "%SRC%"=="" (
  echo Usage: tools\rig.cmd "input.glb" [-Name myasset] [-Preview] [-Screenshot]
  exit /b 2
)
shift
set "REST="
:collect
if "%~1"=="" goto run
set "REST=%REST% %1"
shift
goto collect
:run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%pipeline\rig_asset.ps1" -In "%SRC%" %REST%
exit /b %ERRORLEVEL%
