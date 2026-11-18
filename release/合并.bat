@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set OUT=..\ScreenRecorder-win64-portable.zip
if exist "%OUT%" del "%OUT%"

echo 正在合并 7 个分片...
copy /b part-01+part-02+part-03+part-04+part-05+part-06+part-07 "%OUT%" >nul
if errorlevel 1 (
    echo.
    echo 合并失败：请确认 release 目录下 part-01 ~ part-07 都存在。
    pause
    exit /b 1
)

echo 已生成：%OUT%
echo 正在校验 SHA256...
certutil -hashfile "%OUT%" SHA256 | findstr /v "hash CertUtil"
echo.
echo 期望值：28e8639d7cb836e43418bc7437c7d2ec2f6308da7e05ed1cc18836451bac2bfa
echo 若上面两行一致，解压 zip 后双击 ScreenRecorder.exe 即可使用。
pause
