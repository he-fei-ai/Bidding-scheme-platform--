$pnpmRoot = Join-Path (Get-Location) "node_modules\.pnpm"
$flat = Join-Path (Get-Location) "node_modules"
$count = 0

Get-ChildItem $pnpmRoot -Directory | ForEach-Object {
    $entry = $_
    $srcNodeModules = Join-Path $entry.FullName "node_modules"
    if (Test-Path $srcNodeModules) {
        Get-ChildItem $srcNodeModules -Directory | ForEach-Object {
            $pkgName = $_.Name
            $dst = Join-Path $flat $pkgName
            if (-not (Test-Path (Join-Path $dst "package.json"))) {
                if (-not (Test-Path $dst)) {
                    New-Item -Path $dst -ItemType Directory -Force | Out-Null
                }
                Copy-Item -Path $_.FullName -Destination $dst -Recurse -Force -ErrorAction SilentlyContinue
                $count++
                Write-Host "Copied: $pkgName"
            }
        }
    }
}

Write-Host "Total copied: $count"
