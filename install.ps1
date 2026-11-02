# LiveSync one-line installer for Windows (PowerShell):
#   irm https://raw.githubusercontent.com/dynxssy/livesync/main/install.ps1 | iex
$ErrorActionPreference = "Stop"
$Repo = "dynxssy/livesync"
$Server = if ($env:LIVESYNC_SERVER) { $env:LIVESYNC_SERVER } else { "wss://livesync-relay.onrender.com" }
$UL = if ($env:LIVESYNC_USER_LIBRARY) { $env:LIVESYNC_USER_LIBRARY } else { Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Ableton\User Library" }
$Tmp = Join-Path $env:TEMP ("livesync-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp | Out-Null
Write-Host "Downloading LiveSync..."
Invoke-WebRequest "https://codeload.github.com/$Repo/zip/main" -OutFile "$Tmp\ls.zip"
Expand-Archive "$Tmp\ls.zip" -DestinationPath $Tmp
$Src = Get-ChildItem $Tmp -Directory | Select-Object -First 1
if (-not (Test-Path $UL)) { Write-Host "Ableton User Library not found at $UL. Set LIVESYNC_USER_LIBRARY and run again."; exit 1 }
$RS = Join-Path $UL "Remote Scripts\LiveSync"
if (Test-Path $RS) { Remove-Item $RS -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $UL "Presets\Audio Effects\Max Audio Effect") | Out-Null
Copy-Item (Join-Path $Src.FullName "remote_script\LiveSync") $RS -Recurse
Copy-Item (Join-Path $Src.FullName "m4l\LiveSync.amxd") (Join-Path $UL "Presets\Audio Effects\Max Audio Effect\LiveSync.amxd") -Force
$CfgDir = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "LiveSync"
New-Item -ItemType Directory -Force -Path $CfgDir | Out-Null
$Cfg = Join-Path $CfgDir "settings.json"
$data = @{}
if (Test-Path $Cfg) { try { $data = Get-Content $Cfg -Raw | ConvertFrom-Json -AsHashtable } catch { $data = @{} } }
$data["server"] = $Server
$data | ConvertTo-Json | Set-Content $Cfg -Encoding UTF8
Remove-Item $Tmp -Recurse -Force
Write-Host ""
Write-Host "LiveSync installed. In Ableton Live:"
Write-Host "  1. Quit and reopen Live."
Write-Host "  2. Options > Preferences > Link, Tempo & MIDI > Control Surface: pick LiveSync."
Write-Host "  3. Drag LiveSync (Audio Effects > Max Audio Effect) onto the Master track."
Write-Host "  4. Open an empty Live Set, type your partner's code and press Join."
