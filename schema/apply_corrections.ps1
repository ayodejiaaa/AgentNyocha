param(
    [string]$RootDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\Data Training\Excel Files of the data on Akowe Verify\Excel Files of the data on Akowe Verify",
    [string]$OutDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\corrected",
    [string]$JsonPath = "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\corrections_by_file.json"
)

$byFile = Get-Content $JsonPath -Raw | ConvertFrom-Json

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

function FindCol($headers, [string[]]$candidates) {
    for ($i=0; $i -lt $headers.Count; $i++) {
        $h = ($headers[$i] + "").Trim().ToLower()
        foreach ($c in $candidates) { if ($h -eq $c) { return $i } }
    }
    return -1
}

$totalApplied = 0
$fileKeys = $byFile.PSObject.Properties.Name
Write-Host "Files to correct: $($fileKeys.Count)"

foreach ($fileKey in $fileKeys) {
    $corrections = $byFile.$fileKey
    $srcPath = Join-Path $RootDir $fileKey
    if (-not (Test-Path $srcPath)) { Write-Host "MISSING SOURCE: $srcPath"; continue }

    $destPath = Join-Path $OutDir $fileKey
    $destDir = Split-Path $destPath -Parent
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    Copy-Item -Path $srcPath -Destination $destPath -Force

    # IMPORTANT: force text-format the year/grade/distinction_notes columns on open
    # is not directly controllable via Workbooks.Open for xlsx, so instead we set
    # cell NumberFormat to "@" (text) BEFORE writing each value, which prevents
    # Excel from re-interpreting strings like "2019/2020" as dates.
    $wb = $excel.Workbooks.Open($destPath, $false, $false)
    $ws = $wb.Worksheets.Item(1)
    $used = $ws.UsedRange
    $colCount = $used.Columns.Count
    $headers = @()
    for ($c=1; $c -le $colCount; $c++) { $headers += [string]$ws.Cells.Item(1,$c).Text }

    $yearCol = (FindCol $headers @("year")) + 1
    $gradeCol = (FindCol $headers @("grade")) + 1
    $distCol = (FindCol $headers @("distinction_notes")) + 1

    $needsDistCol = @($corrections | Where-Object { $_.field -eq 'distinction_notes' }).Count -gt 0
    if ($needsDistCol -and $distCol -le 0) {
        $distCol = $colCount + 1
        $ws.Cells.Item(1, $distCol) = "distinction_notes"
        $colCount++
    }

    # Pre-format entire year/grade/distinction_notes columns as text so no
    # value written into them is ever silently reinterpreted as a date.
    if ($yearCol -gt 0) { $ws.Columns.Item($yearCol).NumberFormat = "@" }
    if ($gradeCol -gt 0) { $ws.Columns.Item($gradeCol).NumberFormat = "@" }
    if ($distCol -gt 0) { $ws.Columns.Item($distCol).NumberFormat = "@" }

    $appliedForFile = 0
    foreach ($corr in $corrections) {
        $colIdx = 0
        switch ($corr.field) {
            'year' { $colIdx = $yearCol }
            'grade' { $colIdx = $gradeCol }
            'distinction_notes' { $colIdx = $distCol }
        }
        if ($colIdx -le 0) { continue }
        $ws.Cells.Item([int]$corr.row, $colIdx).Value2 = [string]$corr.newValue
        $appliedForFile++
    }

    $wb.Save()
    $wb.Close($false)
    Write-Host "Applied $appliedForFile corrections to $fileKey"
    $totalApplied += $appliedForFile
}

$excel.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
Write-Host "TOTAL applied: $totalApplied"
