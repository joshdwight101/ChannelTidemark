# Channel Tidemark changelog

## 1.6.1

- Added a one-click MilkDrop 3 support installer that checks dependencies and selects Lactea when ready.

## 1.6.0

- Added optional Lactea rendering for MilkDrop 3 .milk2 double presets in the shared HLS stream.
- Added per-channel engine selection and a checksum-verified Lactea installer with an off-screen render check.

## 1.5.0

- Added a Now playing indicator on the active playlist row and a live elapsed time and progress bar in the main window.
- Late browser viewers join timed YouTube and Vimeo recordings near the server’s current position.

## 1.4.3

- Detect published YouTube and Vimeo durations and schedule provider slots using the detected length.
- Show duration in Link details and read it from the official YouTube player during an embed check when available.
- Look up durations for existing provider items that have no saved duration.

## 1.4.2

- Browser playback requests autoplay with sound and offers a combined playback and video fullscreen control if autoplay is blocked.
- Fullscreen applies to the player and persists across playlist transitions. Open uses the default browser.

## 1.4.1

- Added a direct fullscreen Firefox launch for Browser playback on the desktop, using a dedicated browser profile.

## 1.4.0

- Added a dedicated autoplay browser view with a fullscreen control that stays active across items.
- Moved browser, HLS, and player options URLs into Streaming Links on the main channel screen.
- Defaulted untimed provider items, radio, and live network feeds to five-minute slots so the playlist can repeat.

## 1.3.3

- Added a YouTube embed check that reports actual playback or a provider error in the playlist editor.

## 1.3.2

- Added a prominent Watch on YouTube button when the official iframe player reports an error.

## 1.3.1

- Added official iframe-code import for individual YouTube, Vimeo, and Twitch videos.
- Added browser previews for provider embeds from the playlist item editor.
- Preserved YouTube’s privacy-enhanced iframe host and supported timing and caption settings.

## 1.3.0

- Added native Vimeo and Twitch embeds alongside YouTube.
- Added scheduled external watch pages for Spotify, SoundCloud, and other sites that open on their own players.
- Added first-party sign-in and watch links in the viewer, with guest access by default.
- Added source detection and title lookup for the new providers.

## 1.2.1

- Rebuilt the playlist item editor as a scrollable window with separate Media and Playback sections and visible save and cancel controls.
- Put file selection next to the source field and show link lookup controls for URLs.
- Show validation errors inside the editor and keep the draft open for corrections.

## 1.2.0

- Added asynchronous title suggestions for YouTube, direct media, radio, and webpages.
- Added a custom title override and a control to return to the suggested title.
- Added direct-media resolution for common M3U and PLS radio playlists and webpages with public audio or video metadata.

## 1.1.1

- Recognized pasted YouTube links automatically and preserved optional start timestamps.
- Used the standard YouTube iframe with explicit origin, referrer, and visible-player autoplay handling. YouTube manages controls, ads, and attribution.
- Added embedded-player error messages and a direct **Watch on YouTube** link.

## 1.1.0

- Expanded Channel Settings into a full tab with separate channel, sharing, and removal sections.
- Added an installable headless projectM engine, server-side MilkDrop visuals, a custom preset library, rotation, and texture-folder selection.
- Added per-item MilkDrop overrides for files and media URLs.
- Added optional maximum play times, EOF playback for files, and manual skipping of live items. Earlier timed items now use their saved duration as a maximum.

## 1.0.0

- Added multiple repeating channels with timed media-file, HTTP(S) media, radio, and YouTube items.
- Added FFmpeg HLS output, browser playback, VLC playlist downloads, Kodi STRM downloads, and optional mobile, Roku, and Plex guidance.
- Added unlisted viewer URLs, link rotation, LAN sharing, configurable HTTPS origins, and optional Tailscale Funnel publishing.
- Added a full-window Kiosk Studio viewer link, settings backup and restore, and headless serving.
- Added the branded desktop app, application-menu and desktop launchers, offline Help, and About.
