@echo off
REM One-click verify: GDScript parse check, Godot asset import, then character asset self-test.
REM Usage: tools\verify.cmd [asset-path] [min-bones] [min-clips]
REM Note: keep this file ASCII-only. cmd.exe reads .cmd as ANSI and would mangle CJK text.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "ASSET=%~1"
if "%ASSET%"=="" set "ASSET=res://assets/characters/hero/hero.glb"
set "BONES=%~2"
if "%BONES%"=="" set "BONES=1"
set "CLIPS=%~3"
if "%CLIPS%"=="" set "CLIPS=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%verify.ps1" -Asset "%ASSET%" -MinBones %BONES% -MinClips %CLIPS%
exit /b %ERRORLEVEL%
