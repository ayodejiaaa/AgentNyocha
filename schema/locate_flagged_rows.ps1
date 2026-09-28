param(
    [string]$RootDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\Data Training\Excel Files of the data on Akowe Verify\Excel Files of the data on Akowe Verify",
    [string]$OutCsv = "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\flagged_rows.csv"
)

$gradeMap = Get-Content "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\grade_mapping.json" -Raw | ConvertFrom-Json
$yearMap = Get-Content "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\year_mapping.json" -Raw | ConvertFrom-Json

$flaggedGrades = @{}
foreach ($g in $gradeMap) { if ($g.flag -ne 'ok') { $flaggedGrades[$g.raw] = $g.flag } }

$flaggedYears = @{}
foreach ($y in $yearMap) { if ($y.flag -ne 'ok' -and $y.flag -ne 'ok-single-year') { $flaggedYears[$y.raw] = $y.flag } }

Write-Host "Flagged grade values: $($flaggedGrades.Count)"
Write-Host "Flagged year values: $($flaggedYears.Count)"

$files = Get-ChildItem -Path $RootDir -Filter *.xlsx -Recurse | Sort-Object FullName
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

$results = New-Object System.Collections.Generic.List[object]

function FindCol($headers, [string[]]$candidates) {
    for ($i=0; $i -lt $headers.Count; $i++) {
        $h = ($headers[$i] + "").Trim().ToLower()
        foreach ($c in $candidates) { if ($h -eq $c) { return $i } }
    }
    return -1
}

foreach ($f in $files) {
    $relPath = $f.FullName.Substring($RootDir.Length).TrimStart('\')
    try {
        $wb = $excel.Workbooks.Open($f.FullName, $false, $true)
        foreach ($ws in $wb.Worksheets) {
            $used = $ws.UsedRange
            $rowCount = $used.Rows.Count
            $colCount = $used.Columns.Count
            if ($rowCount -lt 2) { continue }
            $data = $used.Value2
            $headers = @()
            for ($c=1; $c -le $colCount; $c++) { $hv=$data[1,$c]; $headers += [string]$hv }
            $nameCol = (FindCol $headers @("name")) + 1
            $gradeCol = (FindCol $headers @("grade")) + 1
            $yearCol = (FindCol $headers @("year")) + 1
            $pageCol = (FindCol $headers @("page")) + 1
            if ($nameCol -le 0 -or $pageCol -le 0) { continue }

            for ($r=2; $r -le $rowCount; $r++) {
                $nameVal = [string]$data[$r,$nameCol]
                $pageVal = [string]$data[$r,$pageCol]

                if ($gradeCol -gt 0) {
                    $gv = [string]$data[$r,$gradeCol]
                    if ($flaggedGrades.ContainsKey($gv)) {
                        $results.Add([PSCustomObject]@{ File=$relPath; Sheet=$ws.Name; Row=$r; Name=$nameVal; Page=$pageVal; Field="grade"; RawValue=$gv; Flag=$flaggedGrades[$gv] })
                    }
                }
                if ($yearCol -gt 0) {
                    $yv = [string]$data[$r,$yearCol]
                    if ($flaggedYears.ContainsKey($yv)) {
                        $results.Add([PSCustomObject]@{ File=$relPath; Sheet=$ws.Name; Row=$r; Name=$nameVal; Page=$pageVal; Field="year"; RawValue=$yv; Flag=$flaggedYears[$yv] })
                    }
                }
            }
        }
        $wb.Close($false)
    } catch {
        Write-Host "ERROR on $relPath : $($_.Exception.Message)"
    }
    Write-Host "Scanned: $relPath"
}

$excel.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null

$results | Export-Csv -Path $OutCsv -NoTypeInformation -Encoding UTF8
Write-Host "Wrote $OutCsv with $($results.Count) flagged rows"

$byFilePage = $results | Group-Object File, Page | Sort-Object Name
Write-Host "Distinct (file, page) combinations needing a lookup: $($byFilePage.Count)"
