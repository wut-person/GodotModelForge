@echo off
REM One-click demo: reference image -> AI image-to-3D -> retopo -> auto-rig -> Godot render.
REM Usage: tools\demo.cmd [-Image path] [-Name demo_ai] [-Steps 30] [-TargetFaces 9000] [-NoScreenshot] [-Cleanup] [-Pause]
REM Note: keep this file ASCII-only. cmd.exe reads .cmd as ANSI and would mangle CJK text.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%pipeline\demo_pipeline.ps1" %*
exit /b %ERRORLEVEL%
