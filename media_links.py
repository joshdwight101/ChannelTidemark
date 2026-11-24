"""Look up public titles and direct playback links for the playlist editor."""
import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from providers import provider_for, require_url, youtube_clip_duration
from stream_server import source_protocols

MAX_METADATA = 384 * 1024
PLAYLIST_TYPES = {'audio/x-mpegurl', 'audio/mpegurl', 'audio/x-scpls', 'application/x-mpegurl',
                  'application/vnd.apple.mpegurl'}
MEDIA_EXTENSIONS = {
    '.aac', '.aiff', '.asf', '.avi', '.flac', '.flv', '.m4a', '.m4v', '.m3u', '.m3u8',
    '.mkv', '.mov', '.mp2', '.mp3', '.mp4', '.mpd', '.mpeg', '.mpg', '.oga', '.ogg',
    '.ogv', '.opus', '.pls', '.ts', '.wav', '.webm', '.wma', '.wmv',
}
MEDIA_TYPES = PLAYLIST_TYPES | {'application/dash+xml', 'application/ogg', 'application/mp4'}
GENERIC_NAMES = {'audio', 'index', 'live', 'master', 'media', 'playlist', 'radio', 'stream', 'video'}
USER_AGENT = 'ChannelTidemark/1.2'


@dataclass(frozen=True)
class Metadata:
    title: str
    media_url: str = ''
    note: str = ''
    page_only: bool = False
    duration: int | None = None


def published_duration(value):
    if type(value) is int:
        return value if 0 < value <= 604800 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'P(?:(\d{1,2})D)?T(?:(\d{1,4})H)?(?:(\d{1,6})M)?(?:(\d{1,6})S)?', value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return published_duration(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def clean_title(value):
    return ' '.join(value.split())[:150].strip() if isinstance(value, str) else ''


def fallback_title(source):
    parts = urlsplit(source)
    path = unquote(parts.path) if parts.scheme in {'http', 'https'} else source
    name = Path(path).name
    if not parts.scheme or Path(name).suffix.lower() in MEDIA_EXTENSIONS:
        name = Path(name).stem
    if not name or name.casefold() in GENERIC_NAMES:
        name = parts.hostname or name or 'Untitled media'
    return clean_title(name) or 'Untitled media'


class HttpRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        require_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_http(url):
    require_url(url)
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
    return build_opener(HttpRedirects()).open(request, timeout=4)


def small_body(response, max_bytes=MAX_METADATA):
    deadline = time.monotonic() + 5
    data = bytearray()
    while len(data) < max_bytes and time.monotonic() < deadline:
        chunk = response.read1(min(16384, max_bytes - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return data.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')


def media_type(value):
    return value.startswith(('audio/', 'video/')) or value in MEDIA_TYPES


def candidate_url(base, value, mime='', *, media_element=False):
    if not value.strip():
        return ''
    target = urljoin(base, value)
    try:
        require_url(target)
    except ValueError:
        return ''
    if provider_for(target):
        return ''
    mime = mime.split(';', 1)[0].strip().lower()
    if mime and mime not in {'application/octet-stream', 'binary/octet-stream'} and not media_type(mime):
        return ''
    explicit = media_element or media_type(mime)
    return target if explicit or Path(urlsplit(target).path).suffix.lower() in MEDIA_EXTENSIONS else ''


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = {}
        self.sources = []
        self.structured_media = []
        self.json_parts = None
        self.title_parts = []
        self.in_title = False
        self.media_depth = 0

    def handle_starttag(self, tag, attrs):
        values = {key: value or '' for key, value in attrs}
        if tag == 'meta':
            name = (values.get('property') or values.get('name') or values.get('itemprop', '')).lower()
            if name:
                self.tags.setdefault(name, values.get('content', ''))
        if tag == 'title' and not self.title_parts:
            self.in_title = True
        if tag == 'script' and values.get('type', '').lower() == 'application/ld+json':
            self.json_parts = []
        if tag in {'audio', 'video'}:
            self.media_depth += 1
        if self.media_depth and tag in {'audio', 'video', 'source'} and values.get('src'):
            self.sources.append((values['src'], values.get('type', '')))

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag in {'audio', 'video'}:
            self.media_depth = max(0, self.media_depth - 1)
        if tag == 'script' and self.json_parts is not None:
            try:
                self.read_structured_media(json.loads(''.join(self.json_parts)))
            except (ValueError, RecursionError):
                pass
            self.json_parts = None

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.json_parts is not None:
            self.json_parts.append(data)

    def read_structured_media(self, value):
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(reversed(item))
            elif isinstance(item, dict):
                types = item.get('@type', [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and any(
                    isinstance(kind, str) and kind.rsplit('/', 1)[-1] in {'AudioObject', 'VideoObject'}
                    for kind in types
                ):
                    self.structured_media.append(item)
                pending.extend(item.get(key) for key in ('@graph', 'audio', 'video'))

    def title(self):
        for candidate in (self.tags.get('og:title'), self.tags.get('twitter:title'),
                          *(item.get('name') for item in self.structured_media), ''.join(self.title_parts)):
            if title := clean_title(candidate):
                return title
        return ''

    def duration(self):
        if self.tags.get('islivenow', '').lower() == 'true':
            return None
        for value in (self.tags.get('duration'), *(item.get('duration') for item in self.structured_media)):
            if seconds := published_duration(value):
                return seconds
        return None

    def direct_media(self, base):
        for prefix, type_key in (('og:video', 'og:video:type'), ('og:audio', 'og:audio:type'),
                                 ('twitter:player:stream', 'twitter:player:stream:content_type')):
            for key in (f'{prefix}:secure_url', f'{prefix}:url', prefix):
                if value := self.tags.get(key):
                    if target := candidate_url(base, value, self.tags.get(type_key, ''),
                                               media_element=prefix == 'og:audio'):
                        return target
        for source, mime in self.sources:
            if target := candidate_url(base, source, mime, media_element=True):
                return target
        for item in self.structured_media:
            source, mime = item.get('contentUrl'), item.get('encodingFormat', '')
            if isinstance(source, str) and isinstance(mime, str):
                if target := candidate_url(base, source, mime, media_element=True):
                    return target
        return ''


def radio_playlist(body, base):
    lines = [line.strip() for line in body.lstrip('\ufeff').splitlines() if line.strip()]
    if any(line.startswith('#EXT-X-') for line in lines):
        return '', ''
    if lines and lines[0].casefold() == '[playlist]':
        entries = dict(line.split('=', 1) for line in lines if '=' in line)
        fields = {key.strip().lower(): value.strip() for key, value in entries.items()}
        candidates = sorted((key for key in fields if re.fullmatch(r'file\d{1,4}', key)), key=lambda key: int(key[4:]))
        for key in candidates:
            if target := candidate_url(base, fields[key], media_element=True):
                return target, clean_title(fields.get(f'title{key[4:]}'))
    else:
        title = ''
        for line in lines:
            if line.startswith('#EXTINF:') and ',' in line:
                title = clean_title(line.split(',', 1)[1])
            elif not line.startswith('#') and (line.startswith(('http://', 'https://', '/'))
                                               or Path(urlsplit(line).path).suffix.lower() in MEDIA_EXTENSIONS):
                if target := candidate_url(base, line, media_element=True):
                    return target, title
    return '', ''


def probe_metadata(url, ffprobe):
    if not ffprobe:
        return '', None
    try:
        process = subprocess.run(
            [ffprobe, '-v', 'error', '-protocol_whitelist', source_protocols('url'), '-rw_timeout', '3000000',
             '-analyzeduration', '2000000', '-probesize', '524288', '-show_entries',
             'format=duration:format_tags=title,icy-name,ice-name,station:stream_tags=title,icy-name,ice-name,station',
             '-of', 'json', '-i', url],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=6, check=True,
        )
        data = json.loads(process.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return '', None
    try:
        duration = published_duration(math.ceil(float(data.get('format', {}).get('duration', 'nan'))))
    except (ValueError, TypeError, OverflowError):
        duration = None
    for section in (data.get('format', {}), *data.get('streams', [])):
        tags = {key.lower(): value for key, value in section.get('tags', {}).items()}
        for key in ('icy-name', 'ice-name', 'station', 'title'):
            if title := clean_title(tags.get(key)):
                return title, duration
    return '', duration


def look_up_media(url, ffprobe=None):
    require_url(url)
    provider = provider_for(url)
    if provider and provider.key == 'youtube' and provider.embed:
        try:
            with open_http(provider.watch) as response:
                page = PageMetadata()
                page.feed(small_body(response, max_bytes=2 * 1024 * 1024))
                duration = youtube_clip_duration(url, page.duration())
                if duration and (title := page.title()):
                    return Metadata(title, note='Details from YouTube', duration=duration)
        except (OSError, ValueError, LookupError):
            pass
    if provider and provider.oembed:
        query = urlencode({'url': provider.watch, 'format': 'json'})
        try:
            with open_http(f'{provider.oembed}?{query}') as response:
                embedded = json.loads(small_body(response))
                title = clean_title(embedded.get('title')) if isinstance(embedded, dict) else ''
                if title:
                    duration = published_duration(embedded.get('duration')) if provider.key == 'vimeo' else None
                    return Metadata(title, note=f'Details from {provider.name}', duration=duration)
        except (OSError, ValueError, LookupError):
            pass
        url = provider.watch

    try:
        with open_http(url) as response:
            effective_url = response.geturl()
            mime = response.headers.get_content_type()
            station = clean_title(response.headers.get('icy-name') or response.headers.get('ice-name'))
            filename = response.headers.get_filename()
            path = urlsplit(effective_url).path.lower()
            if mime in {'text/html', 'application/xhtml+xml'}:
                page = PageMetadata()
                page.feed(small_body(response))
                title = page.title() or fallback_title(url)
                if provider:
                    return Metadata(title, note=f'Title from the {provider.name} page')
                direct = page.direct_media(effective_url)
                if direct:
                    _, duration = probe_metadata(direct, ffprobe)
                    return Metadata(title, direct, 'The page exposes a direct media link', duration=duration)
                return Metadata(title, note='This page exposes no direct media. Paste its media or stream URL.',
                                page_only=True)
            if mime in PLAYLIST_TYPES or path.endswith(('.m3u', '.m3u8', '.pls')):
                direct, title = radio_playlist(small_body(response), effective_url)
                if direct:
                    suggested, duration = probe_metadata(direct, ffprobe)
                    return Metadata(title or suggested or fallback_title(direct), direct, 'Stream from the playlist',
                                    duration=duration)
    except (OSError, ValueError, LookupError):
        station = ''
        filename = None

    if provider:
        return Metadata(fallback_title(url), note=f'{provider.name} did not supply a title')
    suggested, duration = probe_metadata(url, ffprobe)
    title = station or suggested
    if title:
        return Metadata(title, note='Title from the media stream', duration=duration)
    return Metadata(fallback_title(filename or url), note='Suggested from the filename or URL', duration=duration)
