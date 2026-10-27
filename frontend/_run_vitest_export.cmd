@echo off
cd /d J:\编程\招投标方案平台\frontend
call node scripts\run-vitest.mjs run src/pages/ExportConfigPage.dom.test.tsx --reporter=verbose > ..\logs\_vitest_export_page.txt 2>&1
echo DONE_EXIT_%ERRORLEVEL% >> ..\logs\_vitest_export_page.txt
