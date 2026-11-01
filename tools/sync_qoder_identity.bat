@echo off
rem ============================================================================
rem  把本机 Qoder 客户端的真实机器身份同步到云端面板
rem
rem  为什么需要：国际版每日 100 Credits 是「设备定向」的 —— 平台只发给账号
rem  真正登录的那台机器。云端自己生成的组件身份拿不到活动，必须用本机身份。
rem  runtime-info 每次生成的 token 可能轮换，所以建议每天跑一次（任务计划程序）。
rem
rem  用法：面板地址与口令**不写在**本文件里（本文件会进 git / 部署包）。
rem  把三行配置放到同目录的 sync_qoder_identity.local.bat（已被 .gitignore 排除）：
rem
rem      set PANEL=http://YOUR_SERVER
rem      set PANEL_USER=admin
rem      set PANEL_PASS=你的面板口令
rem
rem  也可以临时用环境变量覆盖。第一次跑本文件若缺少配置，会自动生成模板。
rem  口令忘了？服务器上 grep ADMIN_PASSWORD ~/checkin-hub/.env 即得（用完记得改）。
rem
rem  自动跑：任务计划程序 → 创建基本任务 → 每天 09:50 → 启动程序 → 选本文件
rem ============================================================================
setlocal
set REALM=intl

cd /d "%~dp0"
if not exist "sync_qoder_identity.local.bat" goto make_template

call ".\sync_qoder_identity.local.bat"
cd /d "%~dp0.."

if "%PANEL%"=="" goto missing
if "%PANEL_PASS%"=="" goto missing

".venv\Scripts\python.exe" tools\export_identity.py --realm %REALM% --push %PANEL% --user %PANEL_USER% --password "%PANEL_PASS%"
if errorlevel 1 (
  echo.
  echo [失败] 身份同步失败，请检查面板地址/口令，或查看上面的报错。
) else (
  echo.
  echo [成功] 云端已更新为当前机器的设备身份。
)
goto end

:make_template
echo [提示] 缺少 tools\sync_qoder_identity.local.bat，已生成模板，请填入面板地址与口令后重新双击。
(
echo rem 本文件含面板口令，已在 .gitignore 里排除，不要提交、不要打进部署包。
echo set PANEL=http://YOUR_SERVER_IP
echo set PANEL_USER=admin
echo set PANEL_PASS=CHANGE_ME
) > "sync_qoder_identity.local.bat"
goto end

:missing
echo [失败] 请先在 tools\sync_qoder_identity.local.bat 里填好 PANEL / PANEL_PASS。
pause

:end
endlocal
