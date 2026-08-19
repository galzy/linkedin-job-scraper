# The unattended nightly chain: scrape, refresh, judge fit, export. The exit code is what the scheduler records.
# Copy to nightly.ps1 and set $reports to where the per-day CSVs should land.
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'  # not the console script: uv relinks it and dies on os error 32
$reports = 'C:\path\to\reports'
# The claude CLI lives inside the VS Code extension, whose folder moves on every update: take the newest.
$claude = Get-ChildItem "$env:USERPROFILE\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe" |
    Sort-Object { [version][regex]::Match($_.FullName, '\d+\.\d+\.\d+').Value } |
    Select-Object -Last 1 -ExpandProperty FullName

& $py -m linkedin_job_scraper scrape
$status = $LASTEXITCODE

if ($status -eq 3) {
    Write-Host 'Blocked by LinkedIn; skipping the rest so the next run starts clean.'
    exit $status
}

& $py -m linkedin_job_scraper refresh
if ($status -eq 0) { $status = $LASTEXITCODE }

& $py -m linkedin_job_scraper fit --dest $reports --claude $claude --export-today
if ($status -eq 0) { $status = $LASTEXITCODE }

& $py -m linkedin_job_scraper export  # a jobs.csv left open in Excel must not fail the run

exit $status
