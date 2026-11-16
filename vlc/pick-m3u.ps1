param(
    [ValidateSet("m3u", "epg")]
    [string]$Kind = "m3u",
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding $false
try {
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
    $stdout = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $utf8)
    $stdout.AutoFlush = $true
    [Console]::SetOut($stdout)
} catch {
}

Add-Type -AssemblyName System.Windows.Forms

$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
$dialog.RestoreDirectory = $true
if ($Kind -eq "epg") {
    $dialog.Title = "Choose the XMLTV guide"
    $dialog.Filter = "XMLTV (*.xml;*.xml.gz)|*.xml;*.xml.gz|All files (*.*)|*.*"
} else {
    $dialog.Title = "Choose the M3U list"
    $dialog.Filter = "Playlists (*.m3u;*.m3u8;*.txt)|*.m3u;*.m3u8;*.txt|All files (*.*)|*.*"
}

# VLC's own window stays in front of a dialog that has no owner.
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedToolWindow
$owner.Opacity = 0
$owner.Width = 1
$owner.Height = 1
$owner.Show()
$owner.Activate()
$result = $dialog.ShowDialog($owner)
$owner.Close()
$owner.Dispose()

if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    exit 0
}

$selected = [string]$dialog.FileName
if ([string]::IsNullOrWhiteSpace($selected)) {
    exit 0
}

if (-not [string]::IsNullOrWhiteSpace($OutFile)) {
    [System.IO.File]::WriteAllText($OutFile, $selected, $utf8)
}
[Console]::Out.WriteLine($selected)
exit 0
