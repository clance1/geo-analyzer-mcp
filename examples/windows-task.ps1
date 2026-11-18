# Registers a weekly Windows scheduled task for the full analysis (run in PowerShell as your user).
# Adjust $GeoMcp if geo-mcp isn't on PATH: (Get-Command geo-mcp).Source
$GeoMcp  = (Get-Command geo-mcp).Source
$Out     = "$env:USERPROFILE\geo-reports"
$Action  = New-ScheduledTaskAction -Execute $GeoMcp -Argument "run full-analysis -p local-construction -q --out `"$Out`""
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 7am
Register-ScheduledTask -TaskName "GEO weekly analysis" -Action $Action -Trigger $Trigger -Description "geo-analyzer-mcp" -Force
