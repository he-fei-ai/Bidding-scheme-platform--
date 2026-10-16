Set-Location 'J:\编程\招投标方案平台\frontend'
$p = Start-Process -FilePath 'npx' -ArgumentList 'vitest','run','src/pages/ExportConfigPage.dom.test.tsx','--reporter=basic' -RedirectStandardOutput 'J:\编程\招投标方案平台\frontend\vitest_out.txt' -RedirectStandardError 'J:\编程\招投标方案平台\frontend\vitest_err.txt' -PassThru -NoNewWindow
while (-not $p.HasExited) { Start-Sleep -Seconds 5 }
Write-Output ('Exit code: ' + $p.ExitCode)
Write-Output '--- stdout (last 60 lines) ---'
Get-Content 'J:\编程\招投标方案平台\frontend\vitest_out.txt' | Select-Object -Last 60
Write-Output '--- stderr (last 30 lines) ---'
Get-Content 'J:\编程\招投标方案平台\frontend\vitest_err.txt' | Select-Object -Last 30