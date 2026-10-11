param(
    [ValidateSet("m3u", "epg")]
    [string]$Kind = "m3u"
)

Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.CheckFileExists = $true
if ($Kind -eq "epg") {
    $dialog.Title = "Choose the XMLTV guide"
    $dialog.Filter = "XMLTV (*.xml;*.xml.gz)|*.xml;*.xml.gz|All files (*.*)|*.*"
} else {
    $dialog.Title = "Choose the M3U list"
    $dialog.Filter = "Playlists (*.m3u;*.m3u8)|*.m3u;*.m3u8|All files (*.*)|*.*"
}
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.WriteLine($dialog.FileName)
}
