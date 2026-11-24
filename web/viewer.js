const statusLabel = document.querySelector("#now");
const nextLabel = document.querySelector("#next");
const playButton = document.querySelector("#play");
const cover = document.querySelector("#play-cover");
const video = document.querySelector("video");
const providerPlayer = document.querySelector("#provider-player");
const providerDetails = document.querySelector("#provider-details");
const providerStatus = document.querySelector("#provider-status");
const providerWatch = document.querySelector("#provider-watch");
const providerSignIn = document.querySelector("#provider-sign-in");
const providerHandoff = document.querySelector("#provider-handoff");
const providerOpen = document.querySelector("#provider-open");
const fullscreen = document.querySelector("#fullscreen");
const screen = video?.closest(".screen");
const viewParams = new URLSearchParams(location.search);
const kiosk = Boolean(video) && (document.body.classList.contains("kiosk")
  || viewParams.get("kiosk") === "1");
const autoplay = kiosk || viewParams.get("autoplay") === "1";
if (kiosk) document.body.classList.add("kiosk");
function enterFullscreen() {
  if (screen?.requestFullscreen && !document.fullscreenElement) {
    screen.requestFullscreen().catch(() => {});
  }
}

if (fullscreen && document.fullscreenEnabled && screen?.requestFullscreen) {
  fullscreen.hidden = false;
  fullscreen.addEventListener("click", enterFullscreen);
}
let wanted = autoplay;
let latest = null;
let displayed = null;
let hls = null;
let starting = false;
let youtubeApi = null;
let youtubePlayback = null;

function clearHls() {
  if (hls) {
    hls.destroy();
    hls = null;
  }
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.hidden = true;
}

function stopPlayback() {
  if (!video || displayed === null) return;
  clearHls();
  clearProvider();
  displayed = null;
  starting = false;
  cover.hidden = false;
}

function clearProvider() {
  const playback = youtubePlayback;
  youtubePlayback = null;
  clearTimeout(playback?.readyTimer);
  playback?.observer?.disconnect();
  playback?.player?.destroy();
  providerPlayer.replaceChildren();
  providerPlayer.hidden = true;
  providerDetails.hidden = true;
  providerHandoff.hidden = true;
  providerPlayer.parentElement.classList.remove("provider-screen");
  providerPlayer.parentElement.classList.remove("twitch-screen");
  document.body.classList.remove("provider-active");
}

function loadYouTubeApi() {
  if (typeof window.YT?.Player === "function") return Promise.resolve(window.YT);
  if (!youtubeApi) {
    youtubeApi = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("YouTube API timed out")), 12000);
      window.onYouTubeIframeAPIReady = () => {
        clearTimeout(timeout);
        resolve(window.YT);
      };
      const script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.addEventListener("error", () => {
        clearTimeout(timeout);
        reject(new Error("YouTube API could not load"));
      }, {once: true});
      document.head.append(script);
    });
  }
  return youtubeApi;
}

function tryYouTubeAutoplay(playback) {
  if (youtubePlayback !== playback || !wanted || !playback.ready || !playback.visible
      || playback.attempted || document.visibilityState !== "visible") return;
  playback.attempted = true;
  playback.player.playVideo();
}

function youtubeError(code) {
  const messages = {
    2: "YouTube rejected this video link.",
    5: "YouTube could not play this video in this browser.",
    100: "This video is private, removed, or unavailable on YouTube.",
    101: "The video owner has turned off embedded playback.",
    150: "The video owner has turned off embedded playback.",
    153: "YouTube could not identify this page. Open the viewer link directly in a browser that sends the referrer.",
  };
  return messages[code] || "YouTube reports that this video is unavailable in the embedded player.";
}

function reportEmbedResult(state, result) {
  if (state.check_embed) {
    fetch("embed-result", {method: "POST", body: result}).catch(() => {});
  }
}

function providerFrame(src, name) {
  const frame = document.createElement("iframe");
  frame.src = src;
  frame.title = `${name} player`;
  frame.referrerPolicy = "strict-origin-when-cross-origin";
  frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
  frame.allowFullscreen = true;
  return frame;
}

function presentProvider(name, watch, notice, frame, signIn) {
  cover.hidden = true;
  providerStatus.textContent = notice;
  providerWatch.href = watch;
  providerWatch.textContent = `Watch on ${name}`;
  providerWatch.hidden = !frame;
  providerSignIn.hidden = !signIn;
  if (signIn) {
    providerSignIn.href = signIn;
    providerSignIn.textContent = `Sign in on ${name}`;
  }
  providerDetails.hidden = false;
  providerPlayer.parentElement.classList.add("provider-screen");
  document.body.classList.add("provider-active");
  if (frame) {
    providerPlayer.replaceChildren(frame);
    providerPlayer.hidden = false;
  } else {
    providerOpen.href = watch;
    providerOpen.textContent = name === "YouTube" ? "Watch on YouTube"
      : name === "Original site" ? "Open original site" : `Open ${name}`;
    providerHandoff.hidden = false;
  }
}

function liveOffset(state) {
  if (!state.seekable || !Number.isFinite(state.elapsed) || !Number.isFinite(state.duration)) return 0;
  return Math.max(0, Math.floor(Math.min(state.elapsed, state.duration - 1)));
}

function showYouTube(state) {
  const key = `youtube-${state.slot}-${state.started}`;
  if (displayed === key) return;
  displayed = key;
  starting = false;
  clearHls();
  clearProvider();

  const embed = new URL(state.provider?.embed || `https://www.youtube.com/embed/${state.youtube}`);
  const params = embed.searchParams;
  params.set("enablejsapi", "1");
  params.set("origin", location.origin);
  params.set("playsinline", "1");
  const start = (Number.isSafeInteger(state.youtube_start) ? state.youtube_start : 0) + liveOffset(state);
  if (start > 0) params.set("start", String(start));
  const frame = providerFrame(embed, "YouTube");
  const watch = new URL("https://www.youtube.com/watch");
  watch.searchParams.set("v", state.youtube);
  if (params.has("start")) watch.searchParams.set("t", params.get("start"));
  presentProvider("YouTube", watch, "YouTube", frame, state.provider?.sign_in);

  const playback = {player: null, ready: false, visible: false, attempted: false, observer: null, readyTimer: null};
  youtubePlayback = playback;
  playback.readyTimer = setTimeout(() => {
    if (youtubePlayback === playback) {
      providerStatus.textContent = "The YouTube player is taking longer than expected. Try its controls or open the original video.";
    }
  }, 12000);
  if ("IntersectionObserver" in window) {
    playback.observer = new IntersectionObserver(([entry]) => {
      playback.visible = entry.intersectionRatio > 0.5;
      tryYouTubeAutoplay(playback);
    }, {threshold: [0.5, 0.51]});
    playback.observer.observe(frame);
  }
  loadYouTubeApi().then(api => {
    if (youtubePlayback !== playback) return;
    playback.player = new api.Player(frame, {events: {
      onReady: ({target}) => {
        if (youtubePlayback !== playback) return;
        clearTimeout(playback.readyTimer);
        providerStatus.textContent = state.check_embed ? "Press Play to confirm embedded playback." : "YouTube";
        playback.player = target;
        playback.ready = true;
        tryYouTubeAutoplay(playback);
      },
      onError: ({data}) => {
        if (youtubePlayback === playback) {
          reportEmbedResult(state, [101, 150].includes(data) ? "blocked" : data === 153 ? "unknown" : "failed");
          const restoreFocus = document.activeElement === frame;
          clearProvider();
          presentProvider("YouTube", watch, youtubeError(data), null, state.provider?.sign_in);
          if (restoreFocus) providerOpen.focus();
        }
      },
      onAutoplayBlocked: () => {
        if (youtubePlayback !== playback) return;
        providerStatus.textContent = "Press Play in the YouTube player.";
        if (kiosk && !playback.userAttempt && !document.fullscreenElement) cover.hidden = false;
      },
      onStateChange: ({data, target}) => {
        if (youtubePlayback === playback && data === api.PlayerState.PLAYING) {
          if (!playback.confirmed) {
            const duration = Math.ceil(target.getDuration());
            reportEmbedResult(state, Number.isSafeInteger(duration) && duration > 0 && duration <= 604800
              ? `playing:${duration}` : "playing");
          }
          playback.confirmed = true;
          wanted = true;
          playback.attempted = true;
          cover.hidden = true;
          providerStatus.textContent = "YouTube";
        }
      },
    }});
  }).catch(() => {
    if (youtubePlayback === playback) {
      reportEmbedResult(state, "unknown");
      clearTimeout(playback.readyTimer);
      providerStatus.textContent = "Use the YouTube player controls, or open the original video.";
    }
  });
}

function showProvider(state) {
  const provider = state.provider;
  const watch = new URL(provider.watch);
  if (!["http:", "https:"].includes(watch.protocol)) return;
  const twitch = provider.key === "twitch";
  const twitchReady = location.protocol === "https:" && providerPlayer.parentElement.clientWidth >= 400;
  const inline = Boolean(provider.embed) && (!twitch || twitchReady);
  const key = `provider-${state.slot}-${state.started}-${inline}`;
  if (displayed === key) return;
  displayed = key;
  starting = false;
  clearHls();
  clearProvider();

  let frame = null;
  let notice = `Open ${provider.name === "Original site" ? "the original site" : provider.name} to watch as a guest or with your account.`;
  if (inline) {
    const embed = new URL(provider.embed);
    if (provider.key === "vimeo") {
      const offset = liveOffset(state);
      if (offset > 0) embed.hash = `t=${Math.floor(offset / 60)}m${offset % 60}s`;
    }
    if (twitch) {
      embed.searchParams.set("parent", location.hostname);
      providerPlayer.parentElement.classList.add("twitch-screen");
    }
    if (wanted && (provider.key === "vimeo" || twitch)) {
      embed.searchParams.set("autoplay", twitch ? "true" : "1");
    }
    frame = providerFrame(embed, provider.name);
    notice = provider.name;
  } else if (twitch && provider.embed) {
    notice = "Twitch’s inline player requires HTTPS and a viewer at least 400 pixels wide.";
  }
  presentProvider(provider.name, watch, notice, frame, provider.sign_in);
}

function startHls() {
  video.hidden = false;
  cover.hidden = true;
  starting = true;
  const play = () => {
    starting = false;
    video.play().catch(() => {
      cover.hidden = false;
    });
  };
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = "stream.m3u8";
    play();
  } else if (window.Hls && Hls.isSupported()) {
    hls = new Hls({liveSyncDurationCount: 3});
    hls.on(Hls.Events.MANIFEST_PARSED, play);
    hls.on(Hls.Events.ERROR, (_event, error) => {
      if (error.fatal && displayed === "hls") {
        clearHls();
        displayed = null;
        starting = false;
      }
    });
    hls.loadSource("stream.m3u8");
    hls.attachMedia(video);
  } else {
    starting = false;
    cover.hidden = false;
    playButton.disabled = true;
    playButton.textContent = "Use an HLS-compatible player";
  }
}

function show(state) {
  if (!video) return;
  if (!state.browser || state.status === "offline") {
    stopPlayback();
    return;
  }
  if (state.status === "starting") return;
  if (state.youtube && /^[A-Za-z0-9_-]{11}$/.test(state.youtube)) {
    showYouTube(state);
    return;
  }
  if (state.provider) {
    showProvider(state);
    return;
  }
  if (!providerDetails.hidden) stopPlayback();
  if (!wanted) return;
  if (displayed === "hls" || starting) return;
  displayed = "hls";
  startHls();
}

if (playButton) {
  if (kiosk) cover.hidden = true;
  if (kiosk && document.fullscreenEnabled && screen?.requestFullscreen) {
    playButton.textContent = "Play fullscreen with sound";
  }
  playButton.addEventListener("click", () => {
    wanted = true;
    video.muted = false;
    if (youtubePlayback?.ready) {
      youtubePlayback.userAttempt = true;
      cover.hidden = true;
      youtubePlayback.player.unMute();
      youtubePlayback.player.playVideo();
    } else if (displayed === "hls" && !starting) {
      video.play().then(() => { cover.hidden = true; }).catch(() => {});
    } else if (latest) {
      show(latest);
    }
    if (kiosk) enterFullscreen();
  });
  video.addEventListener("error", () => {
    if (displayed === "hls" && !hls) {
      displayed = null;
      starting = false;
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (youtubePlayback) tryYouTubeAutoplay(youtubePlayback);
});

window.addEventListener("resize", () => {
  if (latest?.provider?.key === "twitch" && latest.browser) show(latest);
});

async function refresh() {
  try {
    const response = await fetch("now.json", {cache: "no-store"});
    if (response.status === 404) stopPlayback();
    if (!response.ok) throw new Error("Channel unavailable");
    latest = await response.json();
    const state = latest;
    if (state.status === "offline") {
      statusLabel.textContent = "Channel offline";
      nextLabel.textContent = "";
    } else {
      const title = state.title || "Channel starting";
      const suffix = state.status === "standby" || state.status === "error" ? " · Standby" : "";
      statusLabel.textContent = title + suffix;
      nextLabel.textContent = state.next ? `Up next · ${state.next}` : "";
    }
    show(state);
  } catch {
    statusLabel.textContent = "Waiting for the channel";
  } finally {
    setTimeout(refresh, 2000);
  }
}

refresh();
