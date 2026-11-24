"""Recognize first-party viewing links and construct official media embeds."""
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
WEB_KINDS = {'youtube', 'provider', 'page'}
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com', 'youtube-nocookie.com'}
EMBED_HOSTS = {'www.youtube.com', 'youtube.com', 'www.youtube-nocookie.com', 'youtube-nocookie.com',
               'player.vimeo.com', 'player.twitch.tv', 'clips.twitch.tv'}


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    watch: str
    sign_in: str = ''
    embed: str = ''
    oembed: str = ''


def require_url(value, *, https_only=False):
    parts = urlsplit(value)
    schemes = ('https',) if https_only else ('http', 'https')
    if (parts.scheme not in schemes or not parts.hostname or parts.username or parts.password
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError('Enter a complete HTTP or HTTPS URL.' if not https_only else 'Enter an HTTPS URL.')
    _ = parts.port
    return parts


def youtube_id(source):
    url = require_url(source)
    route = url.path.strip('/').split('/')
    candidate = ''
    if url.hostname == 'youtu.be':
        candidate = url.path.strip('/')
    elif url.hostname in YOUTUBE_HOSTS:
        if route == ['watch']:
            candidate = parse_qs(url.query).get('v', [''])[0]
        elif len(route) == 2 and route[0] in {'embed', 'shorts', 'live'}:
            candidate = route[1]
    if not VIDEO_ID.fullmatch(candidate):
        raise ValueError('Enter a YouTube link for a single video.')
    return candidate


def youtube_start(source):
    url = urlsplit(source)
    query = parse_qs(url.query)
    fragment = parse_qs(url.fragment)
    value = (query.get('start') or query.get('t') or fragment.get('t') or [''])[0]
    if re.fullmatch(r'[0-9]{1,7}', value):
        return int(value)
    match = re.fullmatch(r'(?:(\d{1,4})h)?(?:(\d{1,4})m)?(?:(\d{1,4})s)?', value)
    if match:
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    return 0


def youtube_clip_duration(source, total):
    if total is None:
        return None
    end = parse_qs(urlsplit(source).query).get('end', [''])[0]
    if re.fullmatch(r'[1-9][0-9]{0,6}', end):
        total = min(total, int(end))
    remaining = total - youtube_start(source)
    return remaining if remaining > 0 else None


def vimeo_provider(url, source):
    route = url.path.strip('/').split('/')
    if url.hostname == 'player.vimeo.com' and len(route) == 2 and route[0] == 'video':
        video = route[1]
        privacy = parse_qs(url.query).get('h', [''])[0]
    elif len(route) in {1, 2}:
        video = route[0]
        privacy = route[1] if len(route) == 2 else parse_qs(url.query).get('h', [''])[0]
    elif (len(route) == 3 and route[0] == 'channels'
          or len(route) == 4 and route[0] == 'groups' and route[2] == 'videos'):
        video, privacy = route[-1], ''
    else:
        return Provider('vimeo', 'Vimeo', source, 'https://vimeo.com/log_in')
    if not re.fullmatch(r'[0-9]{1,15}', video) or (privacy and not re.fullmatch(r'[A-Za-z0-9]{6,24}', privacy)):
        return Provider('vimeo', 'Vimeo', source, 'https://vimeo.com/log_in')
    watch = f'https://vimeo.com/{video}' + (f'/{privacy}' if privacy else '')
    embed = f'https://player.vimeo.com/video/{video}' + ('?' + urlencode({'h': privacy}) if privacy else '')
    return Provider('vimeo', 'Vimeo', watch, 'https://vimeo.com/log_in', embed, 'https://vimeo.com/api/oembed.json')


def twitch_provider(url, source):
    route = url.path.strip('/').split('/')
    query = parse_qs(url.query)
    clip = ''
    if url.hostname == 'clips.twitch.tv' and len(route) == 1:
        clip = query.get('clip', [''])[0] if route[0] == 'embed' else route[0]
    elif len(route) == 3 and route[1] == 'clip':
        clip = route[2]
    if url.hostname == 'player.twitch.tv':
        video = query.get('video', [''])[0].removeprefix('v')
        channel = query.get('channel', [''])[0]
        route = ['videos', video] if video else [channel]
    if re.fullmatch(r'[A-Za-z0-9_-]{1,120}', clip):
        watch = f'https://clips.twitch.tv/{clip}'
        player = 'https://clips.twitch.tv/embed?' + urlencode({'clip': clip, 'autoplay': 'false'})
    elif len(route) == 2 and route[0] == 'videos' and re.fullmatch(r'[0-9]{1,16}', route[1]):
        watch = f'https://www.twitch.tv/videos/{route[1]}'
        player = 'https://player.twitch.tv/?' + urlencode({'video': f'v{route[1]}', 'autoplay': 'false'})
    elif url.hostname != 'clips.twitch.tv' and len(route) == 1 and re.fullmatch(r'[A-Za-z0-9_]{1,25}', route[0]):
        if route[0].lower() in {'directory', 'downloads', 'inventory', 'login', 'settings', 'signup', 'subscriptions'}:
            return Provider('twitch', 'Twitch', source, 'https://www.twitch.tv/login')
        watch = f'https://www.twitch.tv/{route[0]}'
        player = 'https://player.twitch.tv/?' + urlencode({'channel': route[0], 'autoplay': 'false'})
    else:
        return Provider('twitch', 'Twitch', source, 'https://www.twitch.tv/login')
    return Provider('twitch', 'Twitch', watch, 'https://www.twitch.tv/login', player)


def provider_for(source):
    try:
        url = require_url(source)
    except ValueError:
        return None
    host = url.hostname
    if host in YOUTUBE_HOSTS or host == 'youtu.be':
        try:
            video = youtube_id(source)
        except ValueError:
            return Provider('youtube', 'YouTube', source, 'https://www.youtube.com/signin')
        params = {'v': video}
        if start := youtube_start(source):
            params['t'] = start
        embed_host = 'www.youtube-nocookie.com' if host.endswith('youtube-nocookie.com') else 'www.youtube.com'
        embed = f'https://{embed_host}/embed/{video}'
        embed_options = {}
        query = parse_qs(url.query)
        for key, pattern in {
            'end': r'[1-9][0-9]{0,6}',
            'cc_load_policy': r'1',
            'cc_lang_pref': r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?',
            'hl': r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?',
        }.items():
            value = query.get(key, [''])[0]
            if re.fullmatch(pattern, value):
                embed_options[key] = value
        if embed_options:
            embed += '?' + urlencode(embed_options)
        return Provider('youtube', 'YouTube', 'https://www.youtube.com/watch?' + urlencode(params),
                        'https://www.youtube.com/signin', embed,
                        'https://www.youtube.com/oembed')
    if host in {'vimeo.com', 'www.vimeo.com', 'player.vimeo.com'}:
        return vimeo_provider(url, source)
    if host in {'twitch.tv', 'www.twitch.tv', 'm.twitch.tv', 'clips.twitch.tv', 'player.twitch.tv'}:
        return twitch_provider(url, source)
    if host in {'open.spotify.com', 'play.spotify.com', 'spotify.link'}:
        return Provider('spotify', 'Spotify', source, 'https://accounts.spotify.com/login')
    if host in {'soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com', 'on.soundcloud.com'}:
        return Provider('soundcloud', 'SoundCloud', source, 'https://soundcloud.com/login')
    return None


def provider_kind(source):
    provider = provider_for(source)
    if provider is None:
        return ''
    if not provider.embed:
        return 'page'
    return 'youtube' if provider.key == 'youtube' else 'provider'


def scheduled_provider(item):
    if item['kind'] not in WEB_KINDS:
        return None
    provider = provider_for(item['source']) or Provider('page', 'Original site', item['source'])
    return replace(provider, embed='') if item['kind'] == 'page' else provider


class EmbedMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.frames = []

    def handle_starttag(self, tag, attributes):
        if tag == 'iframe':
            attrs = dict(attributes)
            self.frames.append('' if 'srcdoc' in attrs else attrs.get('src') or '')


def imported_embed_url(text):
    text = text.strip()
    if not text or len(text) > 16384:
        raise ValueError('Paste one official iframe snippet or embed URL.')
    if text.startswith('<'):
        parser = EmbedMarkup()
        parser.feed(text)
        if len(parser.frames) != 1 or not parser.frames[0]:
            raise ValueError('Copy the provider’s code for one video iframe.')
        text = parser.frames[0].strip()
    if text.startswith('//'):
        text = 'https:' + text
    url = require_url(text, https_only=True)
    provider = provider_for(text)
    if (url.hostname not in EMBED_HOSTS or url.port not in (None, 443)
            or not provider or not provider.embed
            or (provider.key == 'youtube' and not url.path.startswith('/embed/'))
            or (provider.key == 'vimeo' and not url.path.startswith('/video/'))
            or (url.hostname == 'clips.twitch.tv' and url.path != '/embed')
            or (url.hostname == 'player.twitch.tv' and url.path not in {'', '/'})):
        raise ValueError('Choose an official single-video embed from YouTube, Vimeo, or Twitch.')
    # Store the provider URL alone. The viewer supplies its own iframe and policies.
    return text
