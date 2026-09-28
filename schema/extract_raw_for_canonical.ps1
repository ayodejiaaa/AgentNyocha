param(
    [string]$RootDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\Data Training\Excel Files of the data on Akowe Verify\Excel Files of the data on Akowe Verify",
    [string]$CorrectedDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\corrected",
    [string]$OutDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\schema\raw_extract"
)

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

function FindCol($headers, [string[]]$candidates) {
    for ($i=0; $i -lt $headers.Count; $i++) {
        $h = ($headers[$i] + "").Trim().ToLower()
        foreach ($c in $candidates) { if ($h -eq $c) { return $i } }
    }
    return -1
}

function CsvField([string]$v) {
    if ($null -eq $v) { $v = "" }
    return '"' + $v.Replace('"','""') + '"'
}

$aliases = @{
    name    = @("name")
    faculty = @("faculty","college")
    department = @("department","departemnt","departement")
    course  = @("course")
    degree  = @("degree")
    grade   = @("grade")
    year    = @("year")
    page    = @("page")
    programme = @("program","programme","progam")
    affiliated_institution = @("affiliated institution","affiliated institutions","affiliate institution","affilitated institutions","affiliated school","affiliated program")
    country = @("country")
}
$fieldOrder = @('name','faculty','department','course','degree','grade','year','page','programme','affiliated_institution','country')

$files = Get-ChildItem -Path $RootDir -Filter *.xlsx -Recurse | Sort-Object FullName
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

$manifest = New-Object System.Collections.Generic.List[object]

foreach ($f in $files) {
    $relPath = $f.FullName.Substring($RootDir.Length).TrimStart('\')
    $university = ($relPath -split '\\')[0]

    $correctedPath = Join-Path $CorrectedDir $relPath
    $readPath = if (Test-Path $correctedPath) { $correctedPath } else { $f.FullName }
    $usedCorrected = Test-Path $correctedPath

    try {
        $wb = $excel.Workbooks.Open($readPath, $false, $true)
        $ws = $wb.Worksheets.Item(1)  # main data sheet is always item 1; known junk sheets (FUTA Sheet3, OAU Sheet2) are never item 1
        $used = $ws.UsedRange
        $rowCount = $used.Rows.Count
        $colCount = $used.Columns.Count
        if ($rowCount -lt 2) { $wb.Close($false); continue }
        $data = $used.Value2

        $headers = @()
        for ($c=1; $c -le $colCount; $c++) { $hv = $data[1,$c]; $headers += [string]$hv }

        $colIdx = @{}
        foreach ($field in $aliases.Keys) {
            $colIdx[$field] = (FindCol $headers $aliases[$field]) + 1
        }

        # Known bug: UI 2018's "program" column holds leaked page numbers, not
        # mode-of-study text (the real institution name sits under "affiliated
        # program" instead, which the affiliated_institution alias already
        # catches). Don't trust "program" for this one file.
        $isUI2018 = $relPath -like "*university-of-ibadan-2018*"
        if ($isUI2018) { $colIdx['programme'] = 0 }

        $sb = New-Object System.Text.StringBuilder
        [void]$sb.AppendLine(($fieldOrder -join ','))
        for ($r=2; $r -le $rowCount; $r++) {
            $vals = foreach ($field in $fieldOrder) {
                $ci = $colIdx[$field]
                $v = if ($ci -gt 0) { [string]$data[$r,$ci] } else { '' }
                CsvField $v
            }
            [void]$sb.AppendLine(($vals -join ','))
        }

        $outFile = Join-Path $OutDir (($relPath -replace '[\\/]', '__') + '.csv')
        [System.IO.File]::WriteAllText($outFile, $sb.ToString(), (New-Object System.Text.UTF8Encoding($true)))

        $manifest.Add([PSCustomObject]@{ RelPath = $relPath; University = $university; Rows = ($rowCount-1); UsedCorrected = $usedCorrected; OutFile = $outFile })
        $wb.Close($false)
        Write-Host "Extracted $($rowCount-1) rows from $relPath $(if($usedCorrected){'[corrected]'})"
    } catch {
        Write-Host "ERROR on $relPath : $($_.Exception.Message)"
    }
}

$excel.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null

$manifest | Export-Csv -Path (Join-Path $OutDir "_manifest.csv") -NoTypeInformation -Encoding UTF8
Write-Host "Done. Files: $($manifest.Count)  Total rows: $(($manifest | Measure-Object -Property Rows -Sum).Sum)"
