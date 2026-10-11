# IPTV Guide

A VLC plugin. It searches a large M3U on this PC and plays one channel, so the playlist never goes into VLC's own list.

Python 3 and VLC 3 are required.

## Install

Windows Command Prompt:

```bat
install.bat
```

Git Bash:

```bash
./install.sh
```

The script copies the plugin into `%APPDATA%\vlc\lua\extensions`. Quit VLC completely so it reloads extensions, open VLC again, and choose **View → IPTV Guide**.

Opening the panel starts a local index on this computer. It listens only on localhost.

## Use

Point **Browse** at an M3U file and click **Index**. Open a category, then play one channel. **Public** opens the built-in lists. A channel with no programme falls back to the [Globe TV guides](https://github.com/globetvapp/epg).

[Buy me a coffee](https://donate.stripe.com/dRm4gzd6Ufx49uBfo76g800)
