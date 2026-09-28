param(
    [string]$SrcDir = "C:\Users\User1\Desktop\Dev Projects\Extractor\Canonical Training Data",
    [switch]$MasterOnly,
    [switch]$SkipMaster
)

Add-Type -AssemblyName Microsoft.VisualBasic

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

$RISKY_COLS = @('grade','distinction_notes','year','year_note')  # slash/word patterns Excel might "helpfully" reinterpret

function ConvertOneCsv($csvPath, $xlsxPath) {
    # Parse CSV manually (fast, no type coercion) rather than Import-Csv, since
    # we need raw string arrays for fast bulk-array writes.
    $reader = New-Object System.IO.StreamReader($csvPath, [System.Text.Encoding]::UTF8)
    $parser = New-Object Microsoft.VisualBasic.FileIO.TextFieldParser($reader)
    $parser.SetDelimiters(",")
    $parser.HasFieldsEnclosedInQuotes = $true

    $allRows = New-Object System.Collections.Generic.List[string[]]
    while (-not $parser.EndOfData) {
        $allRows.Add($parser.ReadFields())
    }
    $parser.Close()
    $reader.Close()

    if ($allRows.Count -lt 1) { return $false }
    $headers = $allRows[0]
    $dataRowCount = $allRows.Count - 1
    if ($dataRowCount -lt 1) { return $false }
    $colCount = $headers.Count

    $riskyIdx = @{}
    for ($c=0; $c -lt $colCount; $c++) { if ($RISKY_COLS -contains $headers[$c]) { $riskyIdx[$c] = $true } }

    $wb = $excel.Workbooks.Add()
    $ws = $wb.Worksheets.Item(1)

    # Header row
    for ($c=0; $c -lt $colCount; $c++) { $ws.Cells.Item(1, $c+1) = $headers[$c] }

    # Pre-format risky columns as text for the FULL data range before any write
    foreach ($c in $riskyIdx.Keys) {
        $ws.Columns.Item($c+1).NumberFormat = "@"
    }

    # Bulk-write the non-risky columns via one 2D array assignment (fast)
    $nonRiskyCols = 0..($colCount-1) | Where-Object { -not $riskyIdx.ContainsKey($_) }
    if ($nonRiskyCols.Count -gt 0) {
        foreach ($c in $nonRiskyCols) {
            $arr = New-Object 'object[,]' $dataRowCount,1
            for ($r=0; $r -lt $dataRowCount; $r++) { $arr[$r,0] = $allRows[$r+1][$c] }
            $rng = $ws.Range($ws.Cells.Item(2, $c+1), $ws.Cells.Item(1+$dataRowCount, $c+1))
            $rng.Value2 = $arr
        }
    }

    # Per-cell write for risky columns only, AFTER text format is already set
    foreach ($c in $riskyIdx.Keys) {
        for ($r=0; $r -lt $dataRowCount; $r++) {
            $ws.Cells.Item($r+2, $c+1).Value2 = [string]$allRows[$r+1][$c]
        }
    }

    $destDir = Split-Path $xlsxPath -Parent
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    $wb.SaveAs($xlsxPath, 51)  # 51 = xlOpenXMLWorkbook (.xlsx)
    $wb.Close($false)
    return $true
}

$csvFiles = Get-ChildItem -Path $SrcDir -Filter *.csv -Recurse | Where-Object { $_.Name -ne "akowe_canonical_master.csv" }
if (-not $SkipMaster) {
    $masterCsv = Join-Path $SrcDir "akowe_canonical_master.csv"
}

if (-not $MasterOnly) {
    $i = 0
    foreach ($f in $csvFiles) {
        $i++
        $xlsxPath = $f.FullName -replace '\.csv$', '.xlsx'
        $ok = ConvertOneCsv $f.FullName $xlsxPath
        Write-Host "[$i/$($csvFiles.Count)] $(if($ok){'OK'}else{'SKIP'}): $($f.Name)"
    }
}

if (-not $SkipMaster) {
    Write-Host "Converting master file (this is the big one - may take a while)..."
    $masterXlsx = Join-Path $SrcDir "akowe_canonical_master.xlsx"
    $ok = ConvertOneCsv $masterCsv $masterXlsx
    Write-Host "Master: $(if($ok){'OK'}else{'SKIP'})"
}

$excel.Quit()
[System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null
Write-Host "Done."
