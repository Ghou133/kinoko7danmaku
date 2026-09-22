@echo off
cd /d "%~dp0"
if exist "dist-dots-recovery\Kinoko7Danmaku.exe" (
    start "" "dist-dots-recovery\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-user-services\Kinoko7Danmaku.exe" (
    start "" "dist-user-services\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-voice-library\Kinoko7Danmaku.exe" (
    start "" "dist-voice-library\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-audio-controls\Kinoko7Danmaku.exe" (
    start "" "dist-audio-controls\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-mixed-tts\Kinoko7Danmaku.exe" (
    start "" "dist-mixed-tts\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-ports\Kinoko7Danmaku.exe" (
    start "" "dist-ports\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-performance\Kinoko7Danmaku.exe" (
    start "" "dist-performance\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-user-models\Kinoko7Danmaku.exe" (
    start "" "dist-user-models\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-tts-settings\Kinoko7Danmaku.exe" (
    start "" "dist-tts-settings\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist-streaming\Kinoko7Danmaku.exe" (
    start "" "dist-streaming\Kinoko7Danmaku.exe"
    exit /b
)
if exist "dist\Kinoko7Danmaku.exe" (
    start "" "dist\Kinoko7Danmaku.exe"
    exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "src\main.py"
    exit /b
)
uv run src/main.py
pause
