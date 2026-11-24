# Channel Tidemark

<img src="./io.github.joshuadwight.ChannelTidemark.svg" alt="Channel Tidemark icon" width="96">

An Ubuntu media server with a GTK administration app. Build looping channels from timed playlist items and share one viewer page with multiple devices.

**Version 1.6.1 · Joshua Dwight**

## Install and start

Run the installer from the project folder.

```bash
bash install.sh
```

It uses administrator access to install GTK, libadwaita, and FFmpeg. Open **Channel Tidemark** from the application menu. The admin app runs under your regular account.

1. Create a channel or use **Main channel**. Add local media, direct streams, radio, YouTube, Vimeo, Twitch, or an external watch page. Arrange playback order, and check **Limit play time** for any item that needs a maximum duration.
2. Choose outputs under **Playback Options**, then click **Start Server**. The **Streaming Links** section above the channel tabs provides **Browser playback**, **Direct HLS**, and the **Player options page**.
3. Open the link on a second device on the same network. One FFmpeg encoder per active channel produces a shared HLS stream. Each additional viewer downloads that stream independently.

Files advance at the end or at their configured limit, whichever comes first. A live feed or a provider slot with no limit can hold the channel until you click **Skip Current**. An unavailable source gets a brief standby picture before the next item. Editing a running playlist restarts that channel from its first item.

## Add links and choose titles

In **Add Item…**, paste a URL or choose a file in the **Media** section. Channel Tidemark looks for a published title and fills in **Playlist title**. Edit it to use your own, or choose **Use Suggested Title** to restore the detected name. The **Playback** section has the timer and visual settings. Click **Add Item** in the header when ready.

Supported sources include direct HTTP(S) audio and video files, HLS and DASH manifests, internet radio, and common M3U or PLS radio playlists. Channel Tidemark can also resolve a webpage that exposes a direct audio or video URL in Open Graph, Schema.org metadata, or player markup. Pages that supply their own player can be scheduled as **External watch page** items. [FFmpeg metadata documentation](https://ffmpeg.org/ffprobe.html) · [Open Graph media metadata](https://ogp.me/) · [Schema.org media URLs](https://schema.org/contentUrl)

## Provider playback and accounts

The browser viewer opens **YouTube**, **Vimeo**, and **Twitch** in their official players when the video and viewing conditions allow embedding. Twitch needs an HTTPS viewer at least 400 pixels wide. An inline player is accompanied by a link to the original watch page. Spotify, SoundCloud, and other external watch-page items open directly on the original site.

Guests can open the viewer without a Channel Tidemark account. Open **Provider accounts** to sign in on a service’s own site in a new tab. The provider handles the login, ad delivery, and eligibility for subscription benefits. If the browser or embedded player limits use of the signed-in session, use the original watch page for account playback.

Provider and external-page items are browser options. VLC, Kodi, and other HLS clients show a standby picture for those slots. The channel repeats the playlist automatically. The editor automatically looks up durations published by YouTube and Vimeo. YouTube start and end times shorten the scheduled slot. **Check YouTube Embed** can capture the duration from the official player when the page does not publish it. Files and finite direct links play to the end. Live feeds and browser-only items without a detectable duration use a five-minute slot unless you set **Limit play time**. [YouTube player documentation](https://developers.google.com/youtube/player_parameters) · [Vimeo embed settings](https://help.vimeo.com/hc/en-us/articles/12426259908881-How-to-embed-my-video) · [Twitch embed requirements](https://dev.twitch.tv/docs/embed) · [Spotify widget terms](https://developer.spotify.com/documentation/embeds/terms) · [SoundCloud API terms](https://developers.soundcloud.com/docs/api/terms-of-use)

## MilkDrop visuals

Open **MilkDrop** and select a **Rendering engine**.

- **projectM** is the default. Select **Install projectM…** or run `bash install-milkdrop.sh` to install the Linux renderer for classic `.milk` presets.
- **Lactea · MilkDrop 3** also plays `.milk2` double presets. Click **Install MilkDrop 3 Support…** or run `bash install-lactea.sh` on x86-64 Linux. The installer verifies the pinned release and tests off-screen rendering before activating it. Lactea needs a working Vulkan setup and PyAV. On Ubuntu, these are available through `libvulkan1`, `mesa-vulkan-drivers`, and `python3-av`; a working proprietary Vulkan driver is also supported. Lactea also uses Tidemark’s existing private PyAV installation, if present.

Both engines render on the server without a desktop window. Lactea renders at 256 × 144 and 10 frames per second before the stream encoder scales the visuals, to keep render load manageable on older kiosk hardware. [Lactea MilkDrop 3 compatibility](https://github.com/M0Rf30/lactea/blob/v0.2.2/docs/md3.md)

- Turn on **Use MilkDrop for audio-only items** to visualize audio files, radio, and HTTP(S) feeds without video. The renderer includes the visuals in the shared HLS stream.
- Open a playlist item’s **Visuals** field and choose **Force MilkDrop** to use its audio with MilkDrop frames. Choose **Keep original video** to preserve the original video or the normal static background for an audio-only item.
- Use **Import Preset…** to copy a `.milk` or `.milk2` preset into the local library. Importing a `.milk2` file selects Lactea for the channel settings. Choose a preset or rotate through the imported presets supported by the selected engine. Set the optional texture folder for presets that use external textures. Save the channel’s MilkDrop settings to apply them.

If the renderer is unavailable, playback uses the existing video or a static background. Channel backups store preset choices and paths. Copy your custom preset files and textures separately when moving to a different server.

## Playback compatibility

| Option | What the visitor gets |
| --- | --- |
| Web browser | HLS playback, official provider players, and links to external watch pages |
| Mobile / Direct HLS | The HLS URL for a compatible browser or app |
| VLC | A downloadable M3U playlist containing the HLS URL |
| Kodi | A downloadable STRM file containing the HLS URL |
| Roku | The HLS URL for a Roku channel or compatible player that accepts a stream URL |
| Plex | An M3U feed for a separate compatible tuner bridge |

YouTube items open in YouTube’s standard embedded player, with its controls, ads, and attribution. Paste a watch or share URL, including an optional start time. Visitors see the embed at that point in the video and can follow **Watch on YouTube** to the original page. External HLS players see a standby picture for those slots.

To use YouTube’s own iframe code, choose **Share → Embed → Copy** on the video. In the playlist item editor, select **Paste Embed Code…**, paste the snippet, and click **Use Embed Code**. Click **Check YouTube Embed** to open the official player before saving. Press Play if prompted. The editor reports confirmed playback or a reported embedding restriction. A check that cannot finish is inconclusive. Results reflect the browser used for the check. The importer also accepts official Vimeo and Twitch iframe code. To check Twitch playback or embeds restricted to specific domains, use your published viewer on its real HTTPS address.

If the YouTube player reports an error to the viewer, **Watch on YouTube** takes the place of the player and opens the original watch page in a new tab. The channel stays open in its original tab. To send viewers directly to YouTube for a particular item, choose **External watch page** as its source type after entering the URL. YouTube manages playback, ads, and account sign-in in both viewing modes. Playback switches control what appears on the viewer page. Any recipient with an active HLS link can use a compatible client.

## Share outside the local network

The app automatically shows a local network URL when listening on the LAN. For an automatic public HTTPS address, configure a Tailscale node with Funnel permission, then select **Start Public HTTPS**. The app starts a foreground Funnel on port 8443, points it at the viewer server, and uses the address Tailscale reports. Existing Tailscale configurations on that port take precedence.

For ongoing hosting, set your own HTTPS origin in **File → Network Settings** and forward that origin to the app’s HTTP listener with a reverse proxy or equivalent hosting setup. Leave LAN listening off if the proxy runs on the same computer and is the only intended ingress. Viewers get an unlisted link. Use **Channel Settings → Rotate Link** to revoke the old URL. The HTTP listener serves viewer routes; administration stays in the local GTK app.

## Follow the live channel

The playlist marks **Now playing** on the active row. **Live position** shows elapsed time on the server’s channel clock, with a total and progress bar when a duration is known.

Every viewer follows the server’s playlist order and transition times. Direct HLS viewers join the same live feed. For prerecorded YouTube and Vimeo items with a detected duration, new browser viewers request playback at the current point in the slot. Buffering, provider ads, and external watch pages can put individual viewers behind that clock.

## Use with Kiosk Studio

Enable **Web browser** and copy **Browser playback** from **Streaming Links**. In Kiosk Studio, approve a browser and configure it in **Autostart** with the kiosk link as a browser URL. Add the browser’s kiosk-mode argument if it supports one. Deploy the kiosk profile after checking playback on the target machine. The browser playback page fills the window and requests autoplay with sound. If the browser blocks playback, click **Play fullscreen with sound**. When autoplay is already running, click **Fullscreen** to fill the display. The player stays fullscreen as the playlist advances.

## Keep the server running

The desktop app stops its server when it closes. For a dedicated Ubuntu host, run the installed entry point under a service supervisor as a regular user with access to the media files:

```bash
/usr/bin/python3 "${XDG_DATA_HOME:-$HOME/.local/share}/channel-tidemark/channel_tidemark.py" --serve
```

Headless mode loads the saved settings at startup. Restart that process after editing settings. Set up HTTPS ingress separately for the headless deployment. Plan capacity around the encoder count and outgoing bandwidth, and use media you have permission to distribute.

**File → Back Up Channels** saves the channel list and network settings. Treat that backup as private because it includes link keys and media paths. **File → Install in Application Menu** and **Install Desktop Shortcut** create or refresh launchers. [User guide](./USER-GUIDE.txt) · [Changelog](./changelog.md)

[projectM visualization library](https://github.com/projectM-visualizer/projectm) ·
[YouTube embedded player](https://developers.google.com/youtube/player_parameters) · [Kodi STRM format](https://kodi.wiki/view/Strm) · [Roku streaming specifications](https://developer.roku.com/dev/docs/media) · [Plex tuner documentation](https://support.plex.tv/articles/225877427-supported-dvr-tuners-and-antennas/) · [Tailscale Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel)
