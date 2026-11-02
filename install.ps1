# LiveSync one-line installer for Windows. Paste into Command Prompt or PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/dynxssy/livesync/main/install.ps1 | iex"
$ErrorActionPreference = "Stop"
$Repo = "dynxssy/livesync"
$Server = if ($env:LIVESYNC_SERVER) { $env:LIVESYNC_SERVER } else { "wss://livesync-relay.onrender.com" }
function Find-UserLibrary {
  if ($env:LIVESYNC_USER_LIBRARY) { return $env:LIVESYNC_USER_LIBRARY }
  $cands = @()
  # 1. Where Live itself says the User Library is (Library.cfg in Live's preferences).
  $prefs = Join-Path $env:APPDATA "Ableton"
  if (Test-Path $prefs) {
    Get-ChildItem $prefs -Recurse -Filter "Library.cfg" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending | ForEach-Object {
        $txt = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
        foreach ($m in [regex]::Matches($txt, 'Value="([^"]*User Library[^"]*)"')) {
          $cands += $m.Groups[1].Value.TrimEnd('\')
        }
      }
  }
  # 2. The usual places (including OneDrive-redirected Documents).
  $cands += Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Ableton\User Library"
  $cands += Join-Path $env:USERPROFILE "Documents\Ableton\User Library"
  if ($env:OneDrive) { $cands += Join-Path $env:OneDrive "Documents\Ableton\User Library" }
  foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
  # 3. Live hasn't created one yet: make the default one.
  $def = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Ableton\User Library"
  New-Item -ItemType Directory -Force -Path $def | Out-Null
  Write-Host "No Ableton User Library found, created $def"
  return $def
}
$UL = Find-UserLibrary
Write-Host "Using Ableton User Library: $UL"
$Tmp = Join-Path $env:TEMP ("livesync-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp | Out-Null
Write-Host "Downloading LiveSync..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest "https://codeload.github.com/$Repo/zip/main" -OutFile "$Tmp\ls.zip" -UseBasicParsing
Expand-Archive "$Tmp\ls.zip" -DestinationPath $Tmp
$Src = Get-ChildItem $Tmp -Directory | Select-Object -First 1
$RS = Join-Path $UL "Remote Scripts\LiveSync"
if (Test-Path $RS) { Remove-Item $RS -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $UL "Presets\Audio Effects\Max Audio Effect") | Out-Null
Copy-Item (Join-Path $Src.FullName "remote_script\LiveSync") $RS -Recurse
Copy-Item (Join-Path $Src.FullName "m4l\LiveSync.amxd") (Join-Path $UL "Presets\Audio Effects\Max Audio Effect\LiveSync.amxd") -Force
# Same folder LiveSync's Python uses: %USERPROFILE%\Documents\LiveSync (not the OneDrive-redirected one).
$DocsRoot = Join-Path $env:USERPROFILE "Documents"
if (-not (Test-Path $DocsRoot)) { $DocsRoot = $env:USERPROFILE }
$CfgDir = Join-Path $DocsRoot "LiveSync"
New-Item -ItemType Directory -Force -Path $CfgDir | Out-Null
$Cfg = Join-Path $CfgDir "settings.json"
$data = [ordered]@{}
if (Test-Path $Cfg) {
  try { (Get-Content $Cfg -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $data[$_.Name] = $_.Value } } catch { }
}
$data["server"] = $Server
# Write UTF-8 without a BOM so Python can read it.
[IO.File]::WriteAllText($Cfg, ($data | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
Remove-Item $Tmp -Recurse -Force
Write-Host ""
Write-Host "LiveSync installed. In Ableton Live:"
Write-Host "  1. Quit and reopen Live."
Write-Host "  2. Options > Preferences > Link, Tempo & MIDI > Control Surface: pick LiveSync."
Write-Host "  3. Drag LiveSync (Audio Effects > Max Audio Effect) onto the Master track."
Write-Host "  4. Open an empty Live Set, type your partner's code and press Join."
