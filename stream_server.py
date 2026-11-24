"""Channel scheduling, FFmpeg HLS encoding, and viewer-only HTTP routes."""
import copy
import html
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from milkdrop import engine_available, preset_files
from providers import WEB_KINDS, provider_kind, require_url, scheduled_provider, youtube_clip_duration, youtube_id, youtube_start

WEB = Path(__file__).resolve().parent / 'web'
OUTPUTS = {
    'browser': 'Web browser',
    'mobile': 'Mobile devices',
    'direct': 'Direct HLS link',
    'vlc': 'VLC playlist',
    'kodi': 'Kodi stream file',
    'roku': 'Roku setup information',
    'plex': 'Plex setup information',
}
SOURCES = {
    'file': 'Media file', 'url': 'Media stream URL', 'radio': 'Internet radio',
    'youtube': 'YouTube video', 'provider': 'Provider player', 'page': 'External watch page',
}
VISUAL_OVERRIDES = {'auto': 'Use channel setting', 'milkdrop': 'Force MilkDrop', 'original': 'Keep original video'}
TOKEN = re.compile(r'[A-Za-z0-9_-]{24,48}')
SEGMENT = re.compile(r'segment-\d{10,}\.ts')
DEFAULT_OUTPUTS = ['browser', 'direct', 'mobile', 'vlc', 'kodi']
DEFAULT_LIVE_SECONDS = 300
ASSETS = {
    'viewer.css': 'text/css; charset=utf-8',
    'viewer.js': 'text/javascript; charset=utf-8',
    'hls.light.min.js': 'text/javascript; charset=utf-8',
}


def new_channel(name='New channel'):
    return {'id': secrets.token_hex(8), 'token': secrets.token_urlsafe(24),
            'name': name, 'enabled': True, 'outputs': DEFAULT_OUTPUTS.copy(), 'items': [],
            'milkdrop': milkdrop_settings({})}


def new_config():
    return {'port': 8095, 'lan': True, 'public_base': '', 'channels': [new_channel('Main channel')]}


def validate_item(item):
    if not isinstance(item, dict) or item.get('kind') not in SOURCES:
        raise ValueError('Choose a supported playlist item type.')
    kind = item['kind']
    title = item.get('title', '')
    source = item.get('source', '')
    seconds = item.get('seconds')
    media_duration = item.get('media_duration')
    visual = item.get('visual', 'auto')
    title_auto = item.get('title_auto', False)
    media_url = item.get('media_url', '')
    if (not isinstance(title, str) or not title.strip() or len(title) > 150 or not isinstance(source, str)
            or '\0' in source or not source.strip() or (seconds is not None
            and (type(seconds) is not int or not 1 <= seconds <= 86400))):
        raise ValueError('Enter a title and source. The optional play limit must be between 1 second and 24 hours.')
    if not isinstance(visual, str) or visual not in VISUAL_OVERRIDES:
        raise ValueError('Choose a supported visual override.')
    if not isinstance(title_auto, bool) or not isinstance(media_url, str):
        raise ValueError('The link metadata is invalid.')
    if media_duration is not None and (type(media_duration) is not int or not 1 <= media_duration <= 604800):
        raise ValueError('The detected duration is invalid.')
    source = source.strip()
    if kind != 'page':
        kind = provider_kind(source) or kind
    if kind == 'file':
        source = str(Path(source).expanduser())
        if not Path(source).is_absolute():
            raise ValueError('Choose an absolute media file path.')
    elif kind == 'youtube':
        youtube_id(source)
    else:
        require_url(source)
        if kind == 'provider' and not provider_kind(source):
            raise ValueError('Choose a supported provider URL or select External watch page.')
    media_url = media_url.strip() if kind in {'url', 'radio'} else ''
    if media_url:
        require_url(media_url)
    return {'kind': kind, 'title': title.strip(), 'source': source, 'seconds': seconds, 'visual': visual,
            'media_url': media_url, 'title_auto': title_auto,
            'media_duration': media_duration if kind in {'youtube', 'provider'} else None}


def milkdrop_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('A channel has invalid MilkDrop settings.')
    enabled, rotate, interval = settings.get('enabled', False), settings.get('rotate', False), settings.get('interval', 30)
    preset, textures = settings.get('preset', ''), settings.get('textures', '')
    engine = settings.get('engine', 'projectm')
    extensions = {'.milk', '.milk2'} if engine == 'lactea' else {'.milk'}
    if (not isinstance(enabled, bool) or not isinstance(rotate, bool) or type(interval) is not int
            or not 10 <= interval <= 3600 or not isinstance(preset, str)
            or engine not in ('projectm', 'lactea')
            or (preset and (Path(preset).name != preset or Path(preset).suffix.lower() not in extensions or '\0' in preset))
            or not isinstance(textures, str) or (textures and (not Path(textures).is_absolute() or '\0' in textures))):
        raise ValueError('Check the MilkDrop preset, texture folder, and rotation interval.')
    return {'enabled': enabled, 'preset': preset, 'rotate': rotate, 'interval': interval,
            'textures': textures, 'engine': engine}


def provider_state(item, provider):
    state = {'provider': {'key': provider.key, 'name': provider.name, 'watch': provider.watch,
                          'embed': provider.embed, 'sign_in': provider.sign_in}}
    if item['kind'] == 'youtube':
        state.update(youtube=youtube_id(item['source']), youtube_start=youtube_start(item['source']))
    return state


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('This file does not contain Channel Tidemark settings.')
    port, lan, public = config.get('port'), config.get('lan'), config.get('public_base', '')
    if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(lan, bool) or not isinstance(public, str):
        raise ValueError('Choose a valid listening port and network setting.')
    public = public.strip().rstrip('/')
    if public:
        url = require_url(public, https_only=True)
        if url.path or url.query or url.fragment:
            raise ValueError('Use an HTTPS origin without a path, query, or fragment.')
    channels = config.get('channels')
    if not isinstance(channels, list):
        raise ValueError('The channel list is invalid.')
    identifiers, tokens = set(), set()
    normalized = []
    for channel in channels:
        if not isinstance(channel, dict):
            raise ValueError('Invalid channel.')
        identifier, token, name = (channel.get(key) for key in ('id', 'token', 'name'))
        if (not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{16}', identifier) or identifier in identifiers
                or not isinstance(token, str) or not TOKEN.fullmatch(token) or token in tokens
                or not isinstance(name, str) or not name.strip() or len(name) > 100
                or not isinstance(channel.get('enabled'), bool) or not isinstance(channel.get('items'), list)):
            raise ValueError('A channel has invalid or duplicate settings.')
        outputs = channel.get('outputs')
        if not isinstance(outputs, list) or any(not isinstance(key, str) or key not in OUTPUTS for key in outputs):
            raise ValueError('A channel has an invalid playback option.')
        identifiers.add(identifier)
        tokens.add(token)
        normalized.append({'id': identifier, 'token': token, 'name': name.strip(), 'enabled': channel['enabled'],
                           'outputs': list(dict.fromkeys(outputs)), 'items': list(map(validate_item, channel['items'])),
                           'milkdrop': milkdrop_settings(channel.get('milkdrop', {}))})
    return {'port': port, 'lan': lan, 'public_base': public, 'channels': normalized}


def lan_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        try:
            route.connect(('192.0.2.1', 9))
            return route.getsockname()[0]
        except OSError:
            return '127.0.0.1'


def source_protocols(kind):
    if kind == 'file':
        return 'file,crypto,data'
    return 'http,https,tcp,tls,crypto,data'


def probe_arguments(item):
    return ['-v', 'error', '-protocol_whitelist', source_protocols(item['kind']),
            *(['-rw_timeout', '6000000'] if item['kind'] != 'file' else []),
            '-show_entries', 'format=duration:stream=index,codec_type:stream_disposition=attached_pic', '-of', 'json',
            '-i', item.get('media_url') or item['source']]


def input_arguments(item):
    return ['-re', '-protocol_whitelist', source_protocols(item['kind']),
            *(['-rw_timeout', '6000000'] if item['kind'] != 'file' else []), '-i', item.get('media_url') or item['source']]


def time_limit(seconds):
    return ['-t', f'{seconds:.3f}'] if seconds is not None else []


def encoding_arguments(item, tracks, seconds, sequence=0):
    video, audio = tracks
    args = ['-hide_banner', '-nostdin', '-loglevel', 'warning', '-y']
    input_count = 0
    if item and (video is not None or audio is not None):
        args.extend(input_arguments(item))
        input_count = 1
    maps = [f'0:{video}' if video is not None else None, f'0:{audio}' if audio is not None else None]
    if video is None:
        color = '0x253853' if item and item['kind'] == 'radio' else '0x232537'
        args.extend(['-re', '-f', 'lavfi', '-i', f'color=c={color}:s=960x540:r=25'])
        maps[0] = f'{input_count}:v:0'
        input_count += 1
    if audio is None:
        args.extend(['-re', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000'])
        maps[1] = f'{input_count}:a:0'
    shortest = ['-shortest'] if (video is None) != (audio is None) else []
    return [*args, '-map', maps[0], '-map', maps[1], *time_limit(seconds), *shortest, *hls_arguments(sequence)]


def hls_arguments(sequence):
    return ['-vf', 'scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25',
            '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-threads', '2',
            '-pix_fmt', 'yuv420p', '-g', '100', '-keyint_min', '100', '-sc_threshold', '0',
            '-b:v', '1200k', '-maxrate', '1500k', '-bufsize', '2400k',
            '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '128k',
            '-f', 'hls', '-start_number', str(sequence), '-hls_time', '4', '-hls_list_size', '12', '-hls_delete_threshold', '3',
            '-hls_flags', 'append_list+omit_endlist+independent_segments+temp_file+delete_segments',
            '-hls_segment_filename', 'segment-%010d.ts', 'stream.m3u8']


class ChannelRunner:
    def __init__(self, channel, directory, ffmpeg, ffprobe):
        self.items = channel['items']
        self.milkdrop = channel['milkdrop']
        self.directory = directory
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.stop = threading.Event()
        self.skip_requested = threading.Event()
        self.lock = threading.Lock()
        self.processes = set()
        self.state = {'status': 'starting', 'title': '', 'started': 0, 'seconds': 0, 'slot': 0}
        self.slot_start = None
        self.sequence = time.time_ns() // 1_000_000
        self.thread = threading.Thread(target=self.run, name=f"channel-{channel['id']}", daemon=True)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def snapshot(self):
        with self.lock:
            elapsed = time.monotonic() - self.slot_start if self.slot_start is not None else 0
            return {**self.state, 'elapsed': elapsed}

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.signal_processes()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.signal_processes(kill=True)
            self.thread.join()

    def skip(self):
        self.skip_requested.set()
        self.signal_processes()

    def signal_processes(self, *, kill=False):
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    process.kill() if kill else process.terminate()

    def start_process(self, argv, **options):
        with self.lock:
            if self.stop.is_set() or self.skip_requested.is_set():
                return None
            options.setdefault('stdin', subprocess.DEVNULL)
            process = subprocess.Popen(argv, **options)
            self.processes.add(process)
            return process

    def forget(self, process):
        with self.lock:
            self.processes.discard(process)

    def probe(self, item):
        if item['kind'] in WEB_KINDS:
            return (None, None), None
        process = self.start_process([self.ffprobe, *probe_arguments(item)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if process is None:
            return (None, None), None
        try:
            output, _ = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return (None, None), None
        finally:
            self.forget(process)
        if process.returncode != 0:
            return (None, None), None
        info = json.loads(output)
        streams = info.get('streams', ())
        video = next((track['index'] for track in streams if track.get('codec_type') == 'video'
                      and not track.get('disposition', {}).get('attached_pic')), None)
        audio = next((track['index'] for track in streams if track.get('codec_type') == 'audio'), None)
        try:
            duration = float(info.get('format', {}).get('duration', 'nan'))
        except (TypeError, ValueError):
            duration = 0
        return ((None if item['kind'] == 'radio' else video), audio), (
            duration if math.isfinite(duration) and duration > 0 else None
        )

    def encode(self, item, tracks, seconds):
        with (self.directory / 'encoder.log').open('wb') as log:
            process = self.start_process([self.ffmpeg, *encoding_arguments(item, tracks, seconds, self.sequence)],
                                         cwd=self.directory, stdout=subprocess.DEVNULL, stderr=log)
            if process is None:
                return True
            try:
                return process.wait(timeout=seconds + 10 if seconds is not None else None) == 0
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return False
            finally:
                self.forget(process)

    def show_milkdrop(self, item, tracks):
        video, audio = tracks
        return audio is not None and (item['visual'] == 'milkdrop' or
                                      (item['visual'] == 'auto' and self.milkdrop['enabled'] and video is None))

    def render_milkdrop(self, item, audio, seconds):
        commands = [
            [self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'warning', *input_arguments(item),
             '-map', f'0:{audio}', *time_limit(seconds), '-ac', '2', '-ar', '48000',
             '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
            [sys.executable, str(Path(__file__).with_name('milkdrop.py')), '--engine', self.milkdrop['engine'],
             '--interval', str(self.milkdrop['interval'])],
            [self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'warning', '-y',
             '-f', 'nut', '-i', 'pipe:0', '-map', '0:v:0', '-map', '0:a:0', *hls_arguments(self.sequence)],
        ]
        presets = preset_files(self.milkdrop['engine'])
        if not self.milkdrop['rotate']:
            presets = [path for path in presets if path.name == self.milkdrop['preset']]
        for preset in presets:
            commands[1].extend(['--preset', str(preset)])
        if self.milkdrop['textures']:
            commands[1].extend(['--textures', self.milkdrop['textures']])
        processes = []
        with (self.directory / 'encoder.log').open('wb') as log:
            try:
                previous = None
                for index, command in enumerate(commands):
                    process = self.start_process(command, cwd=self.directory,
                                                 stdin=previous.stdout if previous else subprocess.DEVNULL,
                                                 stdout=subprocess.PIPE if index < 2 else subprocess.DEVNULL, stderr=log)
                    if previous:
                        previous.stdout.close()
                    if process is None:
                        return False
                    processes.append(process)
                    previous = process
                timeout = seconds + 15 if seconds is not None else None
                return processes[-1].wait(timeout=timeout) == 0 and all(
                    process.wait(timeout=3) == 0 for process in processes[:-1]
                )
            except subprocess.TimeoutExpired:
                return False
            finally:
                for process in reversed(processes):
                    if process.stdout:
                        process.stdout.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    self.forget(process)

    def run(self):
        slot = 0
        durations = {}
        while not self.stop.is_set():
            self.skip_requested.clear()
            index = slot % len(self.items)
            item = self.items[index]
            next_item = self.items[(slot + 1) % len(self.items)]
            slot += 1
            try:
                tracks, media_length = self.probe(item)
                if self.stop.is_set():
                    break
                if self.skip_requested.is_set():
                    continue
                provider = scheduled_provider(item)
                unavailable = provider is None and all(track is None for track in tracks)
                visual = self.show_milkdrop(item, tracks)
                rendered = visual and engine_available(self.milkdrop['engine'])
                limit = item['seconds']
                detected = item.get('media_duration')
                if detected is None and provider and provider.embed and provider.key in {'youtube', 'vimeo'}:
                    source = item['source']
                    if source not in durations:
                        # Media metadata resolves playback links using the protocol settings in this module.
                        from media_links import look_up_media
                        durations[source] = look_up_media(source).duration
                    detected = durations[source]
                    if self.stop.is_set() or self.skip_requested.is_set():
                        continue
                if detected is not None:
                    limit = detected if limit is None else min(limit, detected)
                if limit is None and (provider or item['kind'] == 'radio' or (item['kind'] == 'url' and not media_length)):
                    limit = DEFAULT_LIVE_SECONDS
                actual_limit = min(limit or 4, 4) if unavailable else limit
                known_lengths = [value for value in (actual_limit, media_length) if value is not None]
                with self.lock:
                    self.slot_start = time.monotonic()
                    deadline = self.slot_start + limit if limit is not None else None
                    self.state = {
                        'status': 'standby' if unavailable else 'live',
                        'title': item['title'], 'next': next_item['title'], 'started': time.time(),
                        'seconds': limit, 'slot': slot, 'kind': item['kind'],
                        'item_index': index, 'duration': min(known_lengths) if known_lengths else None,
                        'seekable': bool(provider and provider.embed and detected),
                        'visual': 'milkdrop' if rendered else 'standby' if visual else '',
                    }
                    if provider:
                        self.state.update(provider_state(item, provider))
                if rendered:
                    ok = self.render_milkdrop(item, tracks[1], limit)
                    if not ok and not self.stop.is_set() and not self.skip_requested.is_set():
                        with self.lock:
                            self.state['visual'] = 'standby'
                        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
                        ok = self.encode(item, tracks, remaining) if remaining != 0 else True
                else:
                    ok = self.encode(item, tracks, actual_limit)
                if not ok and not self.stop.is_set() and not self.skip_requested.is_set():
                    with self.lock:
                        self.state['status'] = 'standby'
                    remaining = min(4, max(0.0, deadline - time.monotonic())) if deadline is not None else 4
                    if remaining and not self.encode(None, (None, None), remaining):
                        with self.lock:
                            self.state['status'] = 'error'
                        self.stop.wait(2)
            except (OSError, ValueError, KeyError, TypeError):
                with self.lock:
                    self.state['status'] = 'error'
                self.stop.wait(2)


class ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, application):
        self.application = application
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, ViewerRequest)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10)
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class StreamingServer:
    def __init__(self, config, *, ffmpeg=None, ffprobe=None):
        self.config = validate_config(config)
        self.ffmpeg = ffmpeg or shutil.which('ffmpeg')
        self.ffprobe = ffprobe or shutil.which('ffprobe')
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError('Install FFmpeg and FFprobe before starting the stream server.')
        self.lock = threading.RLock()
        self.runners = {}
        self.root = None
        self.http = None
        self.public_override = ''
        self.thread = None

    def start(self):
        if self.http is not None:
            return
        address = '0.0.0.0' if self.config['lan'] else '127.0.0.1'
        self.root = tempfile.TemporaryDirectory(prefix='channel-tidemark-')
        try:
            self.http = ViewerHTTPServer((address, self.config['port']), self)
            self.apply(self.config)
            self.thread = threading.Thread(target=self.http.serve_forever, name='viewer-http', daemon=True)
            self.thread.start()
        except (OSError, RuntimeError, ValueError):
            self.close()
            raise

    def apply(self, config):
        config = validate_config(config)
        with self.lock:
            self.config = config
            active = {c['id']: c for c in config['channels'] if c['enabled'] and c['outputs'] and c['items']}
            for identifier, runner in list(self.runners.items()):
                if (identifier not in active or runner.items != active[identifier]['items']
                        or runner.milkdrop != active[identifier]['milkdrop']):
                    runner.close()
                    shutil.rmtree(runner.directory, ignore_errors=True)
                    del self.runners[identifier]
            for identifier, channel in active.items():
                if identifier not in self.runners:
                    directory = Path(self.root.name) / f'{identifier}-{secrets.token_hex(4)}'
                    runner = ChannelRunner(channel, directory, self.ffmpeg, self.ffprobe)
                    self.runners[identifier] = runner
                    runner.start()

    def find(self, token):
        with self.lock:
            for channel in self.config['channels']:
                if channel['enabled'] and channel['outputs'] and secrets.compare_digest(channel['token'], token):
                    return copy.deepcopy(channel), self.runners.get(channel['id'])
        return None, None

    def origin(self, host=''):
        with self.lock:
            public = self.public_override or self.config['public_base']
            port = self.http.server_port if self.http else self.config['port']
            lan = self.config['lan']
        if host and re.fullmatch(r'[A-Za-z0-9.\-:\[\]]{1,200}', host):
            if public and urlsplit(public).netloc.casefold() == host.casefold():
                return public
            return f'http://{host}'
        if public:
            return public
        return f'http://{lan_address() if lan else "127.0.0.1"}:{port}'

    def close(self):
        if self.http is not None:
            if self.thread is not None and self.thread.is_alive():
                self.http.shutdown()
                self.thread.join()
            self.http.server_close()
        with self.lock:
            for runner in self.runners.values():
                runner.close()
            self.runners.clear()
            if self.root is not None:
                self.root.cleanup()
            self.root = None
            self.http = None
            self.thread = None


class EmbedPreview:
    """Run the same viewer on a private loopback address without starting a stream."""
    directory = None

    def __init__(self, source, title):
        item = validate_item({'kind': 'provider', 'source': source, 'title': title, 'seconds': None})
        provider = scheduled_provider(item)
        if not provider or not provider.embed:
            raise ValueError('Choose an official video embed to preview.')
        self.channel = new_channel(title)
        self.channel.update(outputs=['browser'], items=[item])
        self.result = ''
        self.duration = None
        self.source = source
        self.state = {'status': 'live', 'title': title, 'slot': 1, 'started': time.time(),
                      'check_embed': provider.key == 'youtube',
                      'seconds': None, **provider_state(item, provider)}
        self.http = ViewerHTTPServer(('127.0.0.1', 0), self)
        self.worker = threading.Thread(target=self.http.serve_forever, name='embed-preview', daemon=True)
        self.worker.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_port}/c/{self.channel['token']}/"

    def find(self, token):
        return (self.channel, self) if secrets.compare_digest(self.channel['token'], token) else (None, None)

    def snapshot(self):
        return self.state.copy()

    def close(self):
        self.http.shutdown()
        self.worker.join()
        self.http.server_close()


class ViewerRequest(BaseHTTPRequestHandler):
    def do_POST(self):
        preview = self.server.application
        if (not isinstance(preview, EmbedPreview) or not preview.state['check_embed']
                or self.path != urlsplit(preview.url).path + 'embed-result'
                or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}'):
            self.respond(404, b'', 'text/plain', True)
            return
        length = self.headers.get('Content-Length', '')
        if length not in {str(size) for size in range(6, 15)}:
            self.respond(400, b'', 'text/plain', True)
            return
        result = self.rfile.read(int(length))
        playback = re.fullmatch(rb'playing:([1-9][0-9]{0,5})', result)
        if playback and int(playback[1]) <= 604800:
            if not urlsplit(preview.source).path.startswith('/live/'):
                preview.duration = youtube_clip_duration(preview.source, int(playback[1]))
            result = b'playing'
        if result not in {b'playing', b'blocked', b'failed', b'unknown'}:
            self.respond(400, b'', 'text/plain', True)
            return
        preview.result = result.decode('ascii')
        self.respond(200, b'', 'text/plain', True)

    def log_message(self, *_):
        # Viewer URLs act as access keys. Keep them out of default request logs.
        pass

    def do_HEAD(self):
        self.route(send_body=False)

    def do_GET(self):
        self.route(send_body=True)

    def route(self, *, send_body):
        path = urlsplit(self.path).path
        application = self.server.application
        try:
            if path.startswith('/assets/') and path[8:] in ASSETS:
                name = path[8:]
                self.respond(200, (WEB / name).read_bytes(), ASSETS[name], send_body)
                return
            match = re.fullmatch(r'/c/([A-Za-z0-9_-]{24,48})/(.*)', path)
            if not match:
                self.respond(404, b'Open a shared channel link.', 'text/plain', send_body)
                return
            token, suffix = match.groups()
            channel, runner = application.find(token)
            if not channel:
                self.respond(404, b'Channel unavailable.', 'text/plain', send_body)
                return
            outputs = channel['outputs']
            if not suffix:
                self.respond(200, viewer_page(channel), 'text/html; charset=utf-8', send_body)
            elif suffix == 'watch' and 'browser' in outputs:
                self.respond(200, viewer_page(channel, playback_only=True), 'text/html; charset=utf-8', send_body)
            elif suffix == 'now.json':
                data = runner.snapshot() if runner else {'status': 'offline', 'title': '', 'slot': 0}
                data['browser'] = 'browser' in outputs
                if 'browser' not in outputs:
                    data.pop('youtube', None)
                    data.pop('youtube_start', None)
                    data.pop('provider', None)
                data['time'] = time.time()
                self.respond(200, json.dumps(data).encode(), 'application/json', send_body)
            elif suffix == 'stream.m3u8' or SEGMENT.fullmatch(suffix):
                self.media(runner, suffix, send_body)
            elif suffix in {'vlc.m3u', 'plex.m3u'} and suffix.split('.')[0] in outputs:
                origin = application.origin(self.headers.get('Host', ''))
                title = ' '.join(channel['name'].splitlines())
                data = f'#EXTM3U\n#EXTINF:-1,{title}\n{origin}/c/{token}/stream.m3u8\n'
                self.respond(200, data.encode(), 'audio/x-mpegurl', send_body, download='channel.m3u')
            elif suffix == 'kodi.strm' and 'kodi' in outputs:
                origin = application.origin(self.headers.get('Host', ''))
                data = f'{origin}/c/{token}/stream.m3u8\n'
                self.respond(200, data.encode(), 'text/plain', send_body, download='channel.strm')
            else:
                self.respond(404, b'Playback option unavailable.', 'text/plain', send_body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except (FileNotFoundError, ValueError):
            self.respond(404, b'Resource unavailable.', 'text/plain', send_body)

    def media(self, runner, name, send_body):
        if runner is None or runner.directory is None:
            self.respond(503, b'The channel has no running playlist.', 'text/plain', send_body)
            return
        try:
            data = (runner.directory / name).read_bytes()
        except FileNotFoundError:
            status = 503 if name.endswith('.m3u8') else 404
            self.respond(status, b'Media is not ready.', 'text/plain', send_body)
            return
        content_type = 'application/vnd.apple.mpegurl' if name.endswith('.m3u8') else 'video/mp2t'
        self.respond(200, data, content_type, send_body, cache='private, max-age=20' if name.endswith('.ts') else 'no-store')

    def respond(self, status, data, content_type, send_body, *, download='', cache='no-store'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; "
                         "script-src 'self' https://www.youtube.com https://s.ytimg.com; style-src 'self'; "
                         "connect-src 'self' https://www.youtube.com; media-src 'self' blob:; worker-src blob: 'self'; "
                         "frame-src https://www.youtube.com https://www.youtube-nocookie.com https://player.vimeo.com "
                         "https://player.twitch.tv https://clips.twitch.tv; frame-ancestors 'self'; base-uri 'none'")
        if download:
            self.send_header('Content-Disposition', f'attachment; filename="{download}"')
        self.end_headers()
        if send_body:
            self.wfile.write(data)


def viewer_page(channel, *, playback_only=False):
    name = html.escape(channel['name'])
    outputs = channel['outputs']
    player = ''
    accounts = ''
    if 'browser' in outputs:
        player = '''<section class="screen" aria-label="Channel player">
          <video controls playsinline preload="none" hidden></video>
          <div id="provider-player" hidden></div>
          <div class="play-cover" id="play-cover">
            <button id="play" type="button">Play channel</button>
          </div>
          <div class="provider-handoff" id="provider-handoff" hidden>
            <a id="provider-open" target="_blank" rel="noopener">Open original player</a>
          </div>
        </section>
        <div class="player-actions">
          <button id="fullscreen" type="button" hidden>Fullscreen</button>
        </div>
        <div class="provider-details" id="provider-details" hidden>
          <p id="provider-status" role="status"></p>
          <a id="provider-watch" target="_blank" rel="noopener">Watch on YouTube</a>
          <a id="provider-sign-in" target="_blank" rel="noopener" hidden>Sign in</a>
        </div>'''
        providers = {}
        for item in channel['items']:
            provider = scheduled_provider(item)
            if provider and provider.sign_in:
                providers.setdefault(provider.key, provider)
        if providers:
            links = ''.join(
                f'<a href="{html.escape(provider.sign_in, quote=True)}" target="_blank" rel="noopener">'
                f'Sign in on {html.escape(provider.name)}</a>' for provider in providers.values()
            )
            accounts = ('<details class="accounts"><summary>Provider accounts</summary>'
                        '<p>Watch as a guest or sign in on the service’s own site in a new tab.</p>'
                        f'<nav aria-label="Provider sign-in">{links}</nav></details>')
    cards = {
        'direct': ('Direct HLS', 'Use the live stream URL in an HLS-compatible player.', 'stream.m3u8', 'Open HLS'),
        'vlc': ('VLC', 'Download the playlist and open it in VLC.', 'vlc.m3u', 'Download playlist'),
        'kodi': ('Kodi', 'Download a STRM file for your Kodi video library.', 'kodi.strm', 'Download STRM'),
        'mobile': ('Mobile player', 'Open the HLS link on a phone or copy it into a mobile player.', 'stream.m3u8', 'Open mobile stream'),
        'roku': ('Roku', 'Provide this HLS link to a Roku channel or player that accepts HLS URLs.', 'stream.m3u8', 'Get HLS link'),
        'plex': ('Plex', 'Use this playlist with a compatible tuner bridge that accepts M3U.', 'plex.m3u', 'Download bridge playlist'),
    }
    choices = []
    for key, (title, description, target, label) in cards.items():
        if key in outputs:
            action = f'<a href="{target}">{label}</a>' if target else ''
            choices.append(f'<article><h3>{title}</h3><p>{description}</p>{action}</article>')
    options = ('<section class="players"><h2>Open in another player</h2><div class="choices">'
               + ''.join(choices) + '</div></section>') if choices else ''
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · Channel Tidemark</title><link rel="stylesheet" href="/assets/viewer.css">
<script src="/assets/hls.light.min.js" defer></script><script src="/assets/viewer.js" defer></script></head>
<body class="{'kiosk' if playback_only else ''}"><main><header><p class="brand">Channel Tidemark</p><h1>{name}</h1>
<p id="now" role="status">Loading channel…</p><p class="next" id="next"></p></header>
{player}{'' if playback_only else accounts + options}</main></body></html>'''.encode()
