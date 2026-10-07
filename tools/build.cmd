@echo off
REM One-click: generate a rigged character asset, render a preview, then verify it in Godot.
REM Usage: tools\build.cmd <asset-name> [-Force] [-Preview] [-Height 1.8]
REM Note: keep this file ASCII-only. cmd.exe reads .cmd as ANSI and would mangle CJK text.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%pipeline\build_character.ps1" %* -Verify
exit /b %ERRORLEVEL%
