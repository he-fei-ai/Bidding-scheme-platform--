@echo off
cd /d J:\编程\招投标方案平台\frontend
call node scripts\run-vitest.mjs run src/pages/ExportConfigPage.dom.test.tsx src/utils.wordcount.test.ts src/utils.export.test.ts > ..\logs\_vitest_round_20261021.txt 2>&1
echo DONE_EXIT_%ERRORLEVEL% >> ..\logs\_vitest_round_20261021.txt
