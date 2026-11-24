#!/usr/bin/python3
"""GTK administration window and headless entry point for Channel Tidemark."""
import copy
import json
import math
import secrets
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from media_links import Metadata, fallback_title, look_up_media
from milkdrop import engine_available, import_preset, lactea_dependencies, preset_files
from providers import WEB_KINDS, imported_embed_url, provider_for, provider_kind, require_url
from publishing import PublicFunnel
from stream_server import DEFAULT_LIVE_SECONDS, OUTPUTS, SOURCES, VISUAL_OVERRIDES, StreamingServer, new_channel, new_config
from stream_server import EmbedPreview, validate_config, validate_item

APP_ID = 'io.github.joshuadwight.ChannelTidemark'
APP_NAME = 'Channel Tidemark'
AUTHOR = 'Joshua Dwight'
VERSION = '1.6.1'
DATA = Path(GLib.get_user_data_dir())
CONFIG = Path(GLib.get_user_config_dir()) / 'channel-tidemark' / 'channels.json'
SOURCE = Path(__file__).resolve().parent


def load_config():
    try:
        return validate_config(json.loads(CONFIG.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return new_config()


def save_config(config, path=None):
    path = path or CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(config, stream, indent=2)
            stream.write('\n')
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def desktop_entry():
    key = GLib.KeyFile()
    app_path = str(Path(__file__).resolve())
    escaped = app_path.replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        escaped = escaped.replace(character, '\\' + character)
    icon = SOURCE / f'{APP_ID}.svg'
    for field, value in {
        'Type': 'Application', 'Name': APP_NAME, 'Comment': 'Create and share scheduled media channels',
        'Icon': str(icon) if icon.is_file() else APP_ID, 'Categories': 'AudioVideo;',
        'Exec': f'/usr/bin/python3 "{escaped}"', 'StartupWMClass': APP_ID,
    }.items():
        key.set_string('Desktop Entry', field, value)
    key.set_boolean('Desktop Entry', 'Terminal', False)
    return key.to_data()[0]


def write_launcher(path, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    Gio.File.new_for_path(str(path)).replace_contents(
        desktop_entry().encode(), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None,
    )
    path.chmod(mode)


def make_server(config):
    return StreamingServer(config, ffmpeg=encoder_path('ffmpeg'), ffprobe=encoder_path('ffprobe'))


def encoder_path(name):
    private = SOURCE / 'runtime' / name
    return shutil.which(name) or (str(private) if private.is_file() else None)


def button(label, callback, *, suggested=False):
    widget = Gtk.Button(label=label)
    if suggested:
        widget.add_css_class('suggested-action')
    widget.connect('clicked', callback)
    return widget


def duration_text(seconds):
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    return f'{hours:d}:{minutes:02d}:{rest:02d}'


class LinkTitleEditor:
    """Keep asynchronous link suggestions separate from an administrator's title."""
    def __init__(self, window, save_button, source, title, item, show_watch_page):
        self.save_button, self.source, self.title = save_button, source, title
        self.original_source, self.original_media = item['source'], item.get('media_url', '')
        self.show_watch_page = show_watch_page
        self.manual = bool(item['title'] and not item.get('title_auto'))
        self.filling = self.closed = self.queued = False
        self.timer = None
        self.inflight = ''
        self.result = None
        self.result_url = ''
        self.row = Adw.ActionRow(title='Link details', subtitle_lines=3)
        self.row.set_use_markup(False)
        self.lookup_button = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Look up link details again',
                                        valign=Gtk.Align.CENTER, css_classes=['flat'])
        self.lookup_button.connect('clicked', lambda *_: self.lookup())
        self.use_button = button('Use Suggested Title', self.use_suggestion)
        self.use_button.set_valign(Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.row.add_suffix(self.use_button)
        self.row.add_suffix(self.lookup_button)
        self.row.add_suffix(self.spinner)
        self.title.connect('changed', self.title_changed)
        self.source.connect('changed', self.source_changed)
        window.connect('close-request', self.close)
        self.refresh()

    def url(self):
        text = self.source.get_text().strip()
        try:
            require_url(text)
        except ValueError:
            return ''
        return text

    def title_changed(self, *_):
        if not self.filling:
            self.manual = bool(self.title.get_text().strip())
            self.refresh()

    def source_changed(self, *_):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        self.result = None
        self.result_url = ''
        self.queued = False
        if not self.manual:
            source = self.source.get_text().strip()
            self.set_title(fallback_title(source) if source.startswith(('/', '~')) else '')
        if self.url():
            self.timer = GLib.timeout_add(600, self.debounced_lookup)
        self.refresh()

    def set_title(self, value):
        self.filling = True
        self.title.set_text(value)
        self.manual = False
        self.filling = False

    def refresh(self):
        url = self.url()
        pending = bool(url and (self.timer is not None or self.inflight))
        self.row.set_visible(bool(url))
        self.lookup_button.set_visible(not pending)
        self.spinner.set_visible(pending)
        self.spinner.set_spinning(pending)
        custom_title = bool(self.result and self.result.title and self.title.get_text().strip() != self.result.title)
        self.use_button.set_visible(custom_title)
        if pending:
            subtitle = 'Looking up title, duration, and stream…'
        elif self.result:
            subtitle = 'Opens on the original watch page.' if self.result.page_only else self.result.note
            if self.result.duration:
                subtitle += f' · Duration: {duration_text(self.result.duration)}'
            if self.manual and self.title.get_text().strip() != self.result.title:
                subtitle += f' · Suggested title: {self.result.title}'
        else:
            subtitle = 'Refresh to look up the title and stream again.'
        self.row.set_subtitle(subtitle)
        self.save_button.set_sensitive(bool(self.source.get_text().strip()) and not (pending and not self.manual))

    def debounced_lookup(self):
        self.timer = None
        self.lookup()
        return GLib.SOURCE_REMOVE

    def lookup(self):
        url = self.url()
        if not url or self.closed:
            return
        if self.inflight:
            self.queued = self.inflight != url
            return
        self.inflight = url
        self.refresh()

        def worker():
            try:
                result = look_up_media(url, encoder_path('ffprobe'))
            except (OSError, ValueError, LookupError):
                result = Metadata(fallback_title(url), note='Could not read the link metadata')
            GLib.idle_add(self.deliver, url, result)

        threading.Thread(target=worker, name='link-title-lookup', daemon=True).start()

    def deliver(self, url, result):
        self.inflight = ''
        if self.closed:
            return GLib.SOURCE_REMOVE
        if url == self.url():
            self.result, self.result_url = result, url
            if result.page_only:
                self.show_watch_page()
            if not self.manual:
                self.set_title(result.title)
        queued, self.queued = self.queued, False
        if queued and self.url() and self.url() != url and self.timer is None:
            self.lookup()
        self.refresh()
        return GLib.SOURCE_REMOVE

    def use_suggestion(self, *_):
        if self.result:
            self.set_title(self.result.title)
            self.refresh()

    def media_url(self):
        url = self.url()
        if self.result_url == url and self.result and self.result.media_url:
            return self.result.media_url
        return self.original_media if self.source.get_text().strip() == self.original_source else ''

    def media_duration(self):
        if self.result_url == self.url() and self.result:
            return self.result.duration
        return None

    def close(self, *_):
        self.closed = True
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None


class ChannelTidemark(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.connect('activate', self.activate)
        self.connect('shutdown', lambda *_: self.stop_server())
        self.connect('shutdown', lambda *_: self.close_preview())
        self.server = None
        self.preview = None
        self.funnel = None
        self.chooser = None
        self.engine_install = None
        self.refreshing = False
        self.active_id = None
        self.load_error = ''
        try:
            self.config = load_config()
        except (OSError, ValueError) as error:
            self.load_error = str(error)
            self.config = new_config()

    def activate(self, *_):
        if self.get_active_window():
            self.get_active_window().present()
            return
        self.win = Adw.ApplicationWindow(
            application=self, title=APP_NAME, default_width=1160, default_height=780,
        )
        self.win.set_icon_name(APP_ID)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        brand = Gtk.Box(spacing=8)
        icon = Gtk.Image.new_from_icon_name(APP_ID)
        icon.set_pixel_size(30)
        brand.append(icon)
        brand.append(Adw.WindowTitle(title=APP_NAME, subtitle=f'{AUTHOR} · v{VERSION}'))
        header.set_title_widget(brand)
        self.win.set_content(toolbar)
        toolbar.add_top_bar(header)

        for name, entries in (
            ('File', [
                ('Network Settings…', 'network', self.network_settings),
                ('Back Up Channels…', 'backup', self.backup),
                ('Restore Channels…', 'restore', self.restore),
                ('Install in Application Menu', 'install-menu', self.install_menu),
                ('Install Desktop Shortcut', 'install-shortcut', self.install_shortcut),
            ]),
            ('Help', [('User Guide', 'guide', self.guide), ('About', 'about', self.about)]),
        ):
            items = Gio.Menu()
            for caption, action_name, callback in entries:
                action = Gio.SimpleAction.new(action_name, None)
                action.connect('activate', callback)
                self.add_action(action)
                items.append(caption, f'app.{action_name}')
            header.pack_start(Gtk.MenuButton(label=name, menu_model=items))
        self.server_button = button('Start Server', self.toggle_server, suggested=True)
        header.pack_end(self.server_button)
        self.public_button = button('Start Public HTTPS', self.toggle_public)
        header.pack_end(self.public_button)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ('start', 'end', 'top', 'bottom'):
            getattr(body, f'set_margin_{edge}')(16)
        toolbar.set_content(body)
        self.server_status = Gtk.Label(xalign=0, wrap=True)
        self.server_status.add_css_class('dim-label')
        body.append(self.server_status)

        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=245, vexpand=True, wide_handle=True)
        body.append(panes)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sidebar.append(Gtk.Label(label='Channels', xalign=0, css_classes=['title-3']))
        sidebar.append(button('Add Channel…', lambda *_: self.edit_channel(), suggested=True))
        self.channel_rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=['boxed-list'])
        self.channel_rows.connect('row-selected', self.select_channel)
        sidebar.append(Gtk.ScrolledWindow(child=self.channel_rows, vexpand=True))
        panes.set_start_child(sidebar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        panes.set_end_child(content)
        title_bar = Gtk.Box(spacing=8)
        self.channel_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, css_classes=['title-1'])
        title_bar.append(self.channel_title)
        content.append(title_bar)
        self.channel_status = Gtk.Label(xalign=0, wrap=True, css_classes=['dim-label'])
        content.append(self.channel_status)
        self.playback_position = Gtk.Label(xalign=0, css_classes=['numeric'], visible=False)
        content.append(self.playback_position)
        self.playback_progress = Gtk.ProgressBar(visible=False)
        content.append(self.playback_progress)

        links = Adw.PreferencesGroup(title='Streaming Links')
        self.link_rows = {}
        for path, label in (('watch', 'Browser playback'), ('stream.m3u8', 'Direct HLS'), ('', 'Player options page')):
            row = Adw.ActionRow(title=label, subtitle_selectable=True, subtitle_lines=1)
            copy_button = button('Copy', lambda *_args, suffix=path: self.copy_link(suffix))
            copy_button.set_valign(Gtk.Align.CENTER)
            row.add_suffix(copy_button)
            open_button = None
            if path != 'stream.m3u8':
                open_button = button('Open', lambda *_args, suffix=path: self.open_viewer(suffix))
                open_button.set_valign(Gtk.Align.CENTER)
                row.add_suffix(open_button)
            links.add(row)
            self.link_rows[path] = row, copy_button, open_button
        content.append(links)

        self.tabs = Gtk.Stack(vexpand=True)
        content.append(Gtk.StackSwitcher(stack=self.tabs, halign=Gtk.Align.START))
        content.append(self.tabs)
        playlist = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        actions = Gtk.Box(spacing=8)
        actions.append(button('Add Item…', lambda *_: self.edit_item(), suggested=True))
        actions.append(button('Edit Item…', self.edit_selected))
        actions.append(button('Move Up', lambda *_: self.move_item(-1)))
        actions.append(button('Move Down', lambda *_: self.move_item(1)))
        actions.append(button('Remove…', self.remove_item))
        actions.append(button('Skip Current', self.skip_current))
        playlist.append(actions)
        self.items = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=['boxed-list'])
        self.items.connect('row-activated', lambda _list, row: self.edit_item(row.get_index()))
        playlist.append(Gtk.ScrolledWindow(child=self.items, vexpand=True))
        self.tabs.add_titled(playlist, 'playlist', 'Playlist')

        playback = Adw.PreferencesGroup(title='Viewer playback options',
                                        description='Choose which players and download links appear on the viewer page.')
        self.output_switches = {}
        for key, caption in OUTPUTS.items():
            row = Adw.ActionRow(title=caption)
            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.connect('notify::active', self.change_output, key)
            row.add_suffix(switch)
            row.set_activatable_widget(switch)
            playback.add(row)
            self.output_switches[key] = switch
        self.tabs.add_titled(Gtk.ScrolledWindow(child=playback, vexpand=True), 'playback', 'Playback Options')
        self.tabs.add_titled(self.channel_settings_page(), 'settings', 'Channel Settings')
        self.tabs.add_titled(self.milkdrop_page(), 'milkdrop', 'MilkDrop')

        self.render()
        GLib.timeout_add_seconds(1, self.update_status)
        self.win.present()
        if self.load_error:
            self.message('Could not load saved channels', self.load_error)

    def channel(self):
        return next((channel for channel in self.config['channels'] if channel['id'] == self.active_id), None)

    def changed(self, config):
        try:
            normalized = validate_config(config)
            save_config(normalized)
        except (OSError, ValueError) as error:
            self.message('Could not save channels', str(error))
            return False
        self.config = normalized
        if self.server:
            try:
                self.server.apply(normalized)
            except (OSError, RuntimeError, ValueError) as error:
                self.stop_server()
                self.message('Settings saved. Server stopped.', str(error))
        self.render()
        return True

    @staticmethod
    def clear_rows(listbox):
        while row := listbox.get_row_at_index(0):
            listbox.remove(row)

    def render(self):
        self.refreshing = True
        selected = None
        self.clear_rows(self.channel_rows)
        for channel in self.config['channels']:
            suffix = '' if channel['enabled'] else ' · Paused'
            row = Adw.ActionRow(title=channel['name'], subtitle=f"{len(channel['items'])} items{suffix}")
            row.set_use_markup(False)
            row.channel_id = channel['id']
            self.channel_rows.append(row)
            if channel['id'] == self.active_id:
                selected = row
        row = selected or self.channel_rows.get_row_at_index(0)
        self.channel_rows.select_row(row)
        self.active_id = row.channel_id if row else None
        self.render_channel()
        self.refreshing = False

    def select_channel(self, _list, row):
        if not self.refreshing and row:
            self.active_id = row.channel_id
            self.refreshing = True
            self.render_channel()
            self.refreshing = False

    def render_channel(self):
        channel = self.channel()
        self.channel_title.set_text(channel['name'] if channel else 'Choose or add a channel')
        self.clear_rows(self.items)
        self.playing_row = None
        if channel:
            for item in channel['items']:
                row = Adw.ActionRow(title=item['title'],
                                    subtitle=self.item_summary(item))
                row.set_use_markup(False)
                row.playing_label = Gtk.Label(label='Now playing', css_classes=['accent'],
                                              valign=Gtk.Align.CENTER, visible=False)
                row.add_suffix(row.playing_label)
                self.items.append(row)
        for key, switch in self.output_switches.items():
            switch.set_sensitive(bool(channel))
            switch.set_active(bool(channel and key in channel['outputs']))
        self.render_channel_settings(channel)
        self.render_milkdrop_settings(channel)
        self.update_status()

    @staticmethod
    def item_summary(item):
        seconds = item['seconds']
        detected = item.get('media_duration')
        duration = f'Max {duration_text(seconds)}' if seconds is not None else (
            f'{duration_text(detected)} detected' if detected else
            'Detects duration at playback' if item['kind'] in {'youtube', 'provider'} else
            f'{duration_text(DEFAULT_LIVE_SECONDS)} slot' if item['kind'] in {'page', 'radio'} else 'Plays to the end'
        )
        visual = ' · Force MilkDrop' if item['visual'] == 'milkdrop' else ''
        return f"{SOURCES[item['kind']]} · {duration}{visual}"

    def change_output(self, switch, _param, key):
        channel = self.channel()
        if self.refreshing or not channel:
            return
        updated = copy.deepcopy(self.config)
        copy_channel = next(item for item in updated['channels'] if item['id'] == channel['id'])
        outputs = set(copy_channel['outputs'])
        if switch.get_active():
            outputs.add(key)
        else:
            outputs.discard(key)
        copy_channel['outputs'] = [key for key in OUTPUTS if key in outputs]
        if not self.changed(updated):
            self.render()

    def update_status(self):
        if not hasattr(self, 'link_rows'):
            return GLib.SOURCE_CONTINUE
        running = self.server is not None
        if running:
            origin = self.server.origin()
            visibility = 'Public address' if self.server.public_override or self.config['public_base'] else 'Network address'
            self.server_status.set_text(f'Server running · {visibility} {origin}')
        else:
            self.server_status.set_text('Server stopped · Playlists are saved locally')
        self.server_button.set_label('Stop Server' if running else 'Start Server')
        self.public_button.set_sensitive(running)
        self.public_button.set_label('Stop Public HTTPS' if self.funnel else 'Start Public HTTPS')
        channel = self.channel()
        for path, (row, copy_button, open_button) in self.link_rows.items():
            link = self.share_link(path)
            subtitle = link or ('Turn on Web browser' if running and path == 'watch' and channel
                                and channel['enabled'] and 'browser' not in channel['outputs']
                                else 'Available when the channel is online')
            if row.get_subtitle() != subtitle:
                row.set_subtitle(subtitle)
            copy_button.set_sensitive(bool(link))
            if open_button:
                open_button.set_sensitive(bool(link))
        state = None
        if channel and channel['enabled'] and channel['outputs'] and self.server:
            _, runner = self.server.find(channel['token'])
            state = runner.snapshot() if runner else None
            if state:
                labels = {'live': 'Now playing', 'standby': 'Standby', 'error': 'Encoder error', 'starting': 'Starting'}
                title = f" · {state['title']}" if state['title'] else ''
                visual = {'milkdrop': ' · MilkDrop', 'standby': ' · MilkDrop unavailable'}.get(state.get('visual'), '')
                self.channel_status.set_text(labels[state['status']] + title + visual)
            else:
                self.channel_status.set_text('Add a playlist item to start streaming.')
        elif channel:
            self.channel_status.set_text('Channel paused' if not channel['enabled'] else
                                         'Choose at least one playback option' if not channel['outputs'] else
                                         'Start the server to share this channel.')
        else:
            self.channel_status.set_text('')
        self.update_playback_position(state)
        return GLib.SOURCE_CONTINUE

    def update_playback_position(self, state):
        active = state and state['status'] in {'live', 'standby'} and state.get('item_index') is not None
        row = self.items.get_row_at_index(state['item_index']) if active else None
        if self.playing_row is not row:
            if self.playing_row is not None:
                self.playing_row.playing_label.set_visible(False)
            self.playing_row = row
        if row is not None:
            row.playing_label.set_label('Now playing' if state['status'] == 'live' else 'Standby')
            row.playing_label.set_visible(True)
        duration = state.get('duration') if active else None
        self.playback_position.set_visible(bool(active))
        self.playback_progress.set_visible(bool(duration))
        if active:
            elapsed = state['elapsed']
            if duration:
                elapsed = min(elapsed, duration)
                position = f'{duration_text(int(elapsed))} / {duration_text(math.ceil(duration))}'
                self.playback_progress.set_fraction(elapsed / duration)
            else:
                position = f'{duration_text(int(elapsed))} elapsed'
            self.playback_position.set_text(f'Live position · {position}')

    def share_link(self, suffix=''):
        channel = self.channel()
        if not self.server or not channel or not channel['enabled'] or not channel['outputs']:
            return ''
        if suffix == 'watch' and 'browser' not in channel['outputs']:
            return ''
        return f"{self.server.origin()}/c/{channel['token']}/{suffix}"

    def copy_link(self, suffix):
        link = self.share_link(suffix)
        if link:
            self.win.get_clipboard().set(link)
            self.message('Link copied', link)
        else:
            detail = 'Start the server and select an enabled channel with playback options.'
            self.message('No channel link yet', detail)

    def open_viewer(self, suffix):
        link = self.share_link(suffix)
        if link:
            Gio.AppInfo.launch_default_for_uri(link, None)

    def toggle_server(self, *_):
        if self.server:
            self.stop_server()
            return
        try:
            if not CONFIG.exists():
                save_config(self.config)
            self.server = make_server(self.config)
            self.server.start()
        except (OSError, RuntimeError, ValueError) as error:
            self.server = None
            self.message('Could not start the server', str(error))
        self.update_status()

    def stop_server(self):
        if self.funnel:
            self.funnel.close()
            self.funnel = None
        if self.server:
            self.server.close()
            self.server = None
        self.update_status()

    def toggle_public(self, *_):
        if self.funnel:
            self.funnel.close()
            self.funnel = None
            self.server.public_override = ''
            self.update_status()
            return
        if self.server is None:
            return

        def publish():
            def event(origin, detail):
                GLib.idle_add(self.public_result, tunnel, origin, detail)
            tunnel = PublicFunnel(self.config['port'], event)
            self.funnel = tunnel
            tunnel.start()
            self.update_status()

        self.confirm('Publish viewer links over HTTPS?',
                     'Tailscale Funnel can make your channel links reachable from the internet while the tunnel is running.',
                     'Start Public HTTPS', publish)

    def public_result(self, tunnel, origin, detail):
        if self.funnel is not tunnel or self.server is None:
            return GLib.SOURCE_REMOVE
        self.server.public_override = origin
        if not origin:
            self.funnel = None
        self.update_status()
        self.message('Public sharing' if origin else 'Public sharing unavailable', detail)
        return GLib.SOURCE_REMOVE

    def network_settings(self, *_):
        dialog = self.form('Network Settings')
        group = Adw.PreferencesGroup(description='Stop the server to change the listening port or network binding.')
        port = Adw.SpinRow.new_with_range(1024, 65535, 1)
        port.set_title('HTTP port')
        port.set_value(self.config['port'])
        port.set_sensitive(self.server is None)
        lan = Adw.SwitchRow(title='Listen on the local network', active=self.config['lan'], sensitive=self.server is None)
        base = Adw.EntryRow(title='Public HTTPS origin (optional)', text=self.config['public_base'])
        for row in (port, lan, base):
            group.add(row)
        dialog.set_extra_child(group)

        def save():
            config = copy.deepcopy(self.config)
            config.update(port=int(port.get_value()), lan=lan.get_active(), public_base=base.get_text())
            self.changed(config)
        self.respond_to_form(dialog, save)

    def edit_channel(self):
        dialog = self.form('Add Channel')
        group = Adw.PreferencesGroup()
        entry = Adw.EntryRow(title='Channel name')
        enabled = Adw.SwitchRow(title='Channel enabled', active=True)
        group.add(entry)
        group.add(enabled)
        dialog.set_extra_child(group)

        def save():
            config = copy.deepcopy(self.config)
            updated = new_channel()
            config['channels'].append(updated)
            updated.update(name=entry.get_text(), enabled=enabled.get_active())
            self.active_id = updated['id']
            self.changed(config)
        self.respond_to_form(dialog, save)

    def channel_settings_page(self):
        page = Adw.PreferencesPage()
        basics = Adw.PreferencesGroup(title='Channel Details', description='Set the channel name and viewer availability.')
        self.settings_name = Adw.EntryRow(title='Channel name')
        self.settings_enabled = Adw.SwitchRow(title='Channel enabled')
        basics.add(self.settings_name)
        basics.add(self.settings_enabled)
        save = Adw.ActionRow(title='Apply channel changes')
        save.add_suffix(button('Save Channel Settings', self.save_channel_settings, suggested=True))
        basics.add(save)
        page.add(basics)
        sharing = Adw.PreferencesGroup(title='Sharing', description='Create a new viewer link if the current one should stop working.')
        rotate = Adw.ActionRow(title='Viewer link')
        rotate.add_suffix(button('Rotate Link…', lambda *_: self.channel_action('rotate')))
        sharing.add(rotate)
        page.add(sharing)
        removal = Adw.PreferencesGroup(title='Remove Channel', description='This removes the playlist and its shared links.')
        remove = Adw.ActionRow(title='Remove this channel')
        remove_button = button('Remove Channel…', lambda *_: self.channel_action('remove'))
        remove_button.add_css_class('destructive-action')
        remove.add_suffix(remove_button)
        removal.add(remove)
        page.add(removal)
        self.settings_page = page
        return page

    def render_channel_settings(self, channel):
        self.settings_page.set_sensitive(bool(channel))
        self.settings_name.set_text(channel['name'] if channel else '')
        self.settings_enabled.set_active(bool(channel and channel['enabled']))

    def save_channel_settings(self, *_):
        channel = self.channel()
        if channel:
            updated, item = self.copy_channel(channel['id'])
            item.update(name=self.settings_name.get_text(), enabled=self.settings_enabled.get_active())
            self.changed(updated)

    def channel_action(self, action):
        channel = self.channel()
        if not channel:
            return
        def change():
            updated = copy.deepcopy(self.config)
            if action == 'rotate':
                item = next(c for c in updated['channels'] if c['id'] == channel['id'])
                item['token'] = secrets.token_urlsafe(24)
            else:
                updated['channels'] = [c for c in updated['channels'] if c['id'] != channel['id']]
            self.changed(updated)
        title, note = ('Rotate viewer link?', 'The previous shared URLs will stop working.') if action == 'rotate' else (
            'Remove channel?', 'The playlist and its links will be removed.')
        self.confirm(title, note, 'Rotate Link' if action == 'rotate' else 'Remove', change)

    def milkdrop_page(self):
        page = Adw.PreferencesPage()
        engine = Adw.PreferencesGroup(title='MilkDrop Engine',
                                      description='The server renders one set of visuals for all HLS viewers.')
        quick_setup = Adw.ActionRow(title='MilkDrop 3 presets', subtitle='Enable .milk2 double presets with Lactea.')
        self.lactea_button = button('Install MilkDrop 3 Support…', self.install_lactea)
        self.lactea_button.set_valign(Gtk.Align.CENTER)
        quick_setup.add_suffix(self.lactea_button)
        engine.add(quick_setup)
        self.visual_engine = Adw.ComboRow(title='Rendering engine',
                                          model=Gtk.StringList.new(['projectM', 'Lactea · MilkDrop 3']))
        engine.add(self.visual_engine)
        self.engine_status = Adw.ActionRow(title='Engine availability')
        self.engine_button = button('Install MilkDrop Engine…', self.install_milkdrop)
        self.engine_status.add_suffix(self.engine_button)
        engine.add(self.engine_status)
        page.add(engine)

        channel = Adw.PreferencesGroup(title='Visuals for This Channel')
        self.visual_enabled = Adw.SwitchRow(title='Use MilkDrop for audio-only items',
                                            subtitle='A playlist item can override this choice.')
        self.visual_preset = Adw.ComboRow(title='Preset')
        self.preset_names = ['']
        self.visual_rotate = Adw.SwitchRow(title='Rotate through imported presets')
        self.visual_interval = Adw.SpinRow.new_with_range(10, 3600, 5)
        self.visual_interval.set_title('Seconds per preset')
        self.visual_textures = Adw.EntryRow(title='Optional texture folder')
        texture = Adw.ActionRow(title='Find textures used by custom presets')
        texture.add_suffix(button('Choose Folder…', lambda *_: self.choose_file(
            'Choose Texture Folder', Gtk.FileChooserAction.SELECT_FOLDER,
            lambda path: self.visual_textures.set_text(str(path)),
        )))
        save = Adw.ActionRow(title='Apply visuals to this channel')
        save.add_suffix(button('Save MilkDrop Settings', self.save_milkdrop, suggested=True))
        for row in (self.visual_enabled, self.visual_preset, self.visual_rotate, self.visual_interval,
                    self.visual_textures, texture, save):
            channel.add(row)
        self.visual_group = channel
        self.visual_rotate.bind_property('active', self.visual_interval, 'visible', GObject.BindingFlags.SYNC_CREATE)
        self.visual_rotate.bind_property('active', self.visual_preset, 'sensitive',
                                         GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN)
        page.add(channel)

        library = Adw.PreferencesGroup(title='Preset Library')
        self.preset_library = Adw.ActionRow(title='Custom MilkDrop presets')
        self.preset_library.add_suffix(button('Import Preset…', self.choose_preset))
        library.add(self.preset_library)
        page.add(library)
        self.visual_engine.connect('notify::selected', self.visual_engine_changed)
        return page

    def render_milkdrop_settings(self, channel):
        settings = channel['milkdrop'] if channel else {}
        self.visual_group.set_sensitive(bool(channel))
        self.visual_enabled.set_active(settings.get('enabled', False))
        self.visual_engine.set_selected(int(settings.get('engine') == 'lactea'))
        self.visual_rotate.set_active(settings.get('rotate', False))
        self.visual_interval.set_value(settings.get('interval', 30))
        self.visual_textures.set_text(settings.get('textures', ''))
        self.refresh_presets(settings.get('preset', ''))
        self.refresh_engine_status()

    def refresh_presets(self, selected=''):
        engine = self.selected_visual_engine()
        names = [path.name for path in preset_files(engine)]
        self.preset_names = ['', *names]
        labels = ['Built-in preset', *names]
        if selected and selected not in self.preset_names:
            self.preset_names.append(selected)
            labels.append(f'{selected} (missing)')
        self.visual_preset.set_model(Gtk.StringList.new(labels))
        self.visual_preset.set_selected(self.preset_names.index(selected))
        self.preset_library.set_subtitle(f'{len(names)} imported presets')

    def selected_visual_engine(self):
        return 'lactea' if self.visual_engine.get_selected() == 1 else 'projectm'

    def visual_engine_changed(self, *_):
        selected = self.visual_preset.get_selected()
        name = self.preset_names[selected] if selected < len(self.preset_names) else ''
        if self.selected_visual_engine() == 'projectm' and Path(name).suffix.lower() == '.milk2':
            name = ''
        self.refresh_presets(name)
        self.refresh_engine_status()

    def choose_preset(self, *_):
        def imported(path):
            target = import_preset(path)
            if target.suffix.lower() == '.milk2':
                self.visual_engine.set_selected(1)
            self.refresh_presets(target.name)
        self.choose_file('Import MilkDrop Preset', Gtk.FileChooserAction.OPEN, imported)

    def save_milkdrop(self, *_):
        channel = self.channel()
        if not channel:
            return
        config, updated = self.copy_channel(channel['id'])
        updated['milkdrop'] = {
            'enabled': self.visual_enabled.get_active(), 'rotate': self.visual_rotate.get_active(),
            'engine': self.selected_visual_engine(),
            'interval': int(self.visual_interval.get_value()),
            'preset': self.preset_names[self.visual_preset.get_selected()],
            'textures': self.visual_textures.get_text().strip(),
        }
        self.changed(config)

    def refresh_engine_status(self):
        engine = self.selected_visual_engine()
        name = 'Lactea' if engine == 'lactea' else 'projectM'
        ready = engine_available(engine)
        self.engine_button.set_sensitive(self.engine_install is None)
        self.engine_button.set_visible(engine == 'projectm')
        self.lactea_button.set_sensitive(self.engine_install is None)
        self.lactea_button.set_label('Use MilkDrop 3 Support' if engine_available('lactea')
                                     else 'Install MilkDrop 3 Support…')
        self.engine_button.set_label(f'{"Reinstall" if ready else "Install"} {name}…')
        status = 'Engine installation in progress…' if self.engine_install else (
            f'{name} is available. Accepts .milk and .milk2.' if ready and engine == 'lactea' else
            f'{name} is available. Accepts .milk.' if ready else f'Install {name} to render visuals.'
        )
        self.engine_status.set_subtitle(status)

    def install_milkdrop(self, *_):
        if self.engine_install:
            return

        def install():
            dependencies = ['build-essential', 'cmake', 'curl', 'libgl1-mesa-dev', 'mesa-common-dev', 'libegl1', 'python3-av']
            command = ['/usr/bin/pkexec', '/usr/bin/apt-get', 'install', '--yes', '--no-install-recommends', *dependencies]
            self.run_engine_command(command, lambda: self.run_engine_command(
                ['/usr/bin/bash', str(SOURCE / 'install-milkdrop.sh'), '--skip-deps'], self.engine_installed,
            ))
        self.confirm('Install the MilkDrop engine?',
                     'Install Ubuntu dependencies, then build projectM 4 in your user data directory.',
                     'Install', install)

    def install_lactea(self, *_):
        if self.engine_install:
            return
        if engine_available('lactea'):
            self.visual_engine.set_selected(1)
            return

        def ready():
            self.visual_engine.set_selected(1)
            self.message('MilkDrop 3 presets ready', 'Import a .milk2 preset and save your channel settings.')

        def install():
            self.run_engine_command(['/usr/bin/bash', str(SOURCE / 'install-lactea.sh')], ready)

        packages = lactea_dependencies()
        if packages:
            command = ['/usr/bin/pkexec', '/usr/bin/apt-get', 'install', '--yes', '--no-install-recommends', *packages]
            self.confirm('Install MilkDrop 3 support?', 'Install the required Ubuntu packages, then download and test Lactea.',
                         'Install', lambda: self.run_engine_command(command, install))
        else:
            install()

    def engine_installed(self):
        self.run_engine_command([sys.executable, str(SOURCE / 'milkdrop.py'), '--check'],
                                lambda: self.message('MilkDrop engine ready', 'The off-screen renderer passed its check.'))

    def run_engine_command(self, argv, callback):
        try:
            self.engine_install = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE | Gio.SubprocessFlags.STDIN_PIPE,
            )
        except GLib.Error as error:
            self.message('Could not start installation', str(error))
            return
        self.refresh_engine_status()

        def finished(process, result):
            self.engine_install = None
            self.refresh_engine_status()
            try:
                _, output, errors = process.communicate_utf8_finish(result)
                if process.get_successful():
                    callback()
                else:
                    self.message('MilkDrop setup failed', (errors or output or 'The command did not complete.')[-1800:])
            except GLib.Error as error:
                self.message('MilkDrop setup failed', str(error))
        self.engine_install.communicate_utf8_async('', None, finished)

    def close_preview(self):
        if self.preview:
            self.preview.close()
            self.preview = None

    def paste_embed(self, parent, on_import):
        dialog = Adw.Window(title='Paste Embed Code', transient_for=parent, modal=True,
                            destroy_with_parent=True, default_width=560, default_height=340)
        toolbar = Adw.ToolbarView()
        dialog.set_content(toolbar)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(button('Cancel', lambda *_: dialog.close()))
        use = Gtk.Button(label='Use Embed Code', css_classes=['suggested-action'])
        header.pack_end(use)
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                          margin_bottom=18, margin_start=18, margin_end=18)
        toolbar.set_content(content)
        content.append(Gtk.Label(label='Paste the iframe code from the video’s Share → Embed menu.',
                                 xalign=0, wrap=True))
        code = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8,
                            bottom_margin=8, left_margin=8, right_margin=8)
        scroll = Gtk.ScrolledWindow(vexpand=True, has_frame=True)
        scroll.set_child(code)
        content.append(scroll)
        error = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=['error'])
        content.append(error)

        def accept(*_):
            buffer = code.get_buffer()
            try:
                url = imported_embed_url(buffer.get_text(*buffer.get_bounds(), False))
            except ValueError as failure:
                error.set_text(str(failure))
                error.set_visible(True)
                return
            on_import(url)
            dialog.close()

        use.connect('clicked', accept)
        dialog.present()
        code.grab_focus()

    def edit_item(self, index=None):
        channel = self.channel()
        if not channel:
            self.message('Add a channel first', 'Create a channel to start its playlist.')
            return
        item = channel['items'][index] if index is not None else {
            'kind': 'file', 'title': '', 'source': '', 'seconds': None, 'visual': 'auto',
        }
        heading = 'Add Playlist Item' if index is None else 'Edit Playlist Item'
        editor = Adw.Window(title=heading, transient_for=self.win, modal=True, destroy_with_parent=True,
                            default_width=700, default_height=610)
        toolbar = Adw.ToolbarView()
        editor.set_content(toolbar)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title=heading, subtitle=channel['name']))
        header.pack_start(button('Cancel', lambda *_: editor.close()))
        save_button = Gtk.Button(label='Add Item' if index is None else 'Save Changes',
                                 css_classes=['suggested-action'])
        header.pack_end(save_button)
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        media = Adw.PreferencesGroup(title='Media', description='Paste a media link or choose a local file.')
        kinds = list(SOURCES)
        kind = Adw.ComboRow(title='Source type', model=Gtk.StringList.new(list(SOURCES.values())))
        kind.set_selected(kinds.index(item['kind']))
        title = Adw.EntryRow(title='Playlist title', text=item['title'])
        source = Adw.EntryRow(title='Media file or URL', text=item['source'])
        browse = Gtk.Button(label='Choose File…', valign=Gtk.Align.CENTER, css_classes=['flat'])
        browse.connect('clicked', lambda *_: self.choose_file(
            'Choose Media', Gtk.FileChooserAction.OPEN,
            lambda path: (source.set_text(str(path)), kind.set_selected(0)), parent=editor,
        ))
        source.add_suffix(browse)
        embed_row = Adw.ActionRow(title='Official embed', subtitle='Preview opens in your browser.', subtitle_lines=3)
        paste = button('Paste Embed Code…', lambda *_: self.paste_embed(editor, source.set_text))
        preview = Gtk.Button(label='Preview Embed', valign=Gtk.Align.CENTER)
        paste.set_valign(Gtk.Align.CENTER)
        embed_row.add_suffix(paste)
        embed_row.add_suffix(preview)
        playback = Adw.PreferencesGroup(title='Playback')
        timer_row = Adw.ActionRow(title='Limit play time', subtitle='Use the detected duration by default. Untimed live sources get 5 minutes.')
        timer = Gtk.CheckButton(active=item['seconds'] is not None, valign=Gtk.Align.CENTER)
        timer_row.add_suffix(timer)
        timer_row.set_activatable_widget(timer)
        duration = Adw.SpinRow.new_with_range(1, 86400, 1)
        duration.set_title('Maximum seconds')
        duration.set_value(item['seconds'] or 60)
        timer.bind_property('active', duration, 'visible', GObject.BindingFlags.SYNC_CREATE)
        choices = list(VISUAL_OVERRIDES)
        visuals = Adw.ComboRow(title='Visuals', model=Gtk.StringList.new(list(VISUAL_OVERRIDES.values())))
        visuals.set_selected(choices.index(item['visual']))
        visuals.set_visible(item['kind'] not in WEB_KINDS)
        kind.connect('notify::selected', lambda *_: visuals.set_visible(kinds[kind.get_selected()] not in WEB_KINDS))

        def recognize_url(*_):
            text = source.get_text().strip()
            if text.startswith(('http://', 'https://')):
                recognized = provider_kind(text)
                if recognized:
                    kind.set_selected(kinds.index(recognized))
                elif kinds[kind.get_selected()] != 'radio':
                    kind.set_selected(kinds.index('url'))

        error_label = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=['error'],
                                margin_start=18, margin_end=18, margin_top=10, margin_bottom=10)
        toolbar.add_bottom_bar(error_label)

        def update_preview(*_):
            try:
                provider = provider_for(source.get_text().strip())
            except ValueError:
                provider = None
            enabled = bool(provider and provider.embed and kinds[kind.get_selected()] != 'page')
            youtube = enabled and provider.key == 'youtube'
            preview.set_sensitive(enabled)
            preview.set_label('Check YouTube Embed' if youtube else 'Preview Embed')
            embed_row.set_subtitle('Check playback in your browser before saving.' if youtube
                                   else 'Preview opens in your browser.')

        checked_source = ''
        checked_duration = None

        def track_embed_check(active, url):
            deadline = GLib.get_monotonic_time() + 60 * GLib.USEC_PER_SEC
            embed_row.set_subtitle('Checking in your browser. Press Play if prompted.')

            def poll():
                nonlocal checked_source, checked_duration
                if (self.preview is not active or source.get_text().strip() != url
                        or not editor.get_visible() or kinds[kind.get_selected()] == 'page'):
                    return GLib.SOURCE_REMOVE
                messages = {
                    'playing': 'Embedded playback confirmed in your browser.',
                    'blocked': 'The owner disabled embedding. Use External watch page to open YouTube.',
                    'failed': 'YouTube could not play this embed. Try the original watch page.',
                    'unknown': 'Playback could not be confirmed. Try the published viewer or the original watch page.',
                }
                if active.result in messages:
                    notice = messages[active.result]
                    if active.result == 'playing' and active.duration:
                        checked_source, checked_duration = url, active.duration
                        notice += f' Duration: {duration_text(active.duration)}.'
                    embed_row.set_subtitle(notice)
                elif GLib.get_monotonic_time() < deadline:
                    return GLib.SOURCE_CONTINUE
                else:
                    embed_row.set_subtitle(messages['unknown'])
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(300, poll)

        def open_preview(*_):
            self.close_preview()
            try:
                url = source.get_text().strip()
                self.preview = EmbedPreview(url, title.get_text().strip()[:100] or 'Embed preview')
                Gio.AppInfo.launch_default_for_uri(self.preview.url, None)
                if self.preview.state['check_embed']:
                    track_embed_check(self.preview, url)
            except (OSError, ValueError, GLib.Error) as error:
                self.close_preview()
                error_label.set_text(str(error))
                error_label.set_visible(True)

        source.connect('changed', recognize_url)
        source.connect('changed', update_preview)
        kind.connect('notify::selected', update_preview)
        preview.connect('clicked', open_preview)
        update_preview()
        link_title = LinkTitleEditor(editor, save_button, source, title, item,
                                     lambda: kind.set_selected(kinds.index('page')))
        for row in (source, embed_row, kind, title, link_title.row):
            media.add(row)
        for row in (timer_row, duration, visuals):
            playback.add(row)
        page.add(media)
        page.add(playback)
        for control in (source, title):
            control.connect('changed', lambda *_: error_label.set_visible(False))
        kind.connect('notify::selected', lambda *_: error_label.set_visible(False))

        def save(*_):
            try:
                path = source.get_text().strip()
                name = title.get_text().strip() or fallback_title(path)
                detected = link_title.media_duration()
                if not detected and path == checked_source:
                    detected = checked_duration
                if not detected and path == item['source']:
                    detected = item.get('media_duration')
                entry = validate_item({'kind': kinds[kind.get_selected()], 'title': name,
                                       'source': path, 'visual': choices[visuals.get_selected()],
                                       'title_auto': not link_title.manual, 'media_url': link_title.media_url(),
                                       'media_duration': detected,
                                       'seconds': int(duration.get_value()) if timer.get_active() else None})
                if entry['kind'] == 'file' and not Path(entry['source']).is_file():
                    raise ValueError('Choose a file available on this machine.')
            except (OSError, ValueError) as error:
                error_label.set_text(str(error))
                error_label.set_visible(True)
                return
            config, updated = self.copy_channel(channel['id'])
            if index is None:
                updated['items'].append(entry)
            else:
                updated['items'][index] = entry
            if self.changed(config):
                row = self.items.get_row_at_index(index if index is not None else len(updated['items']) - 1)
                self.items.select_row(row)
                editor.close()

        save_button.connect('clicked', save)
        editor.present()
        source.grab_focus()

    def copy_channel(self, identifier):
        updated = copy.deepcopy(self.config)
        return updated, next(channel for channel in updated['channels'] if channel['id'] == identifier)

    def edit_selected(self, *_):
        row = self.items.get_selected_row()
        if row:
            self.edit_item(row.get_index())

    def move_item(self, direction):
        row, channel = self.items.get_selected_row(), self.channel()
        if row and channel:
            old = row.get_index()
            new = old + direction
            if 0 <= new < len(channel['items']):
                config, updated = self.copy_channel(channel['id'])
                updated['items'][old], updated['items'][new] = updated['items'][new], updated['items'][old]
                if self.changed(config):
                    self.items.select_row(self.items.get_row_at_index(new))

    def remove_item(self, *_):
        row, channel = self.items.get_selected_row(), self.channel()
        if not row or not channel:
            return
        index = row.get_index()
        def remove():
            config, updated = self.copy_channel(channel['id'])
            del updated['items'][index]
            self.changed(config)
        self.confirm('Remove playlist item?', channel['items'][index]['title'], 'Remove', remove)

    def skip_current(self, *_):
        channel = self.channel()
        if self.server and channel:
            _, runner = self.server.find(channel['token'])
            if runner:
                runner.skip()

    def backup(self, *_):
        self.choose_file('Back Up Channels', Gtk.FileChooserAction.SAVE,
                         lambda path: save_config(self.config, path), suggested='channel-tidemark.json')

    def restore(self, *_):
        def load(path):
            config = validate_config(json.loads(path.read_text(encoding='utf-8')))
            if self.server and any(config[key] != self.config[key] for key in ('port', 'lan')):
                self.message('Stop the server first', 'The backup uses different network settings.')
                return
            self.confirm('Restore channel settings?', 'Replace the saved channel list and playback options.',
                         'Restore', lambda: self.changed(config))
        self.choose_file('Restore Channels', Gtk.FileChooserAction.OPEN, load)

    def choose_file(self, title, action, callback, *, suggested=None, parent=None):
        label = {
            Gtk.FileChooserAction.SAVE: 'Save',
            Gtk.FileChooserAction.SELECT_FOLDER: 'Choose Folder',
        }.get(action, 'Open')
        chooser = Gtk.FileChooserNative.new(title, parent or self.win, action, label, 'Cancel')
        chooser.set_modal(True)
        if suggested:
            chooser.set_current_name(suggested)
        self.chooser = chooser
        def result(dialog, response):
            file = dialog.get_file() if response == Gtk.ResponseType.ACCEPT else None
            dialog.destroy()
            self.chooser = None
            if file:
                path = file.get_path()
                if path is None:
                    self.message('Choose a local file', 'Local files are required for this action.')
                    return
                try:
                    callback(Path(path))
                except (OSError, ValueError, GLib.Error) as error:
                    self.message('Could not use the selected file', str(error))
        chooser.connect('response', result)
        chooser.show()

    def form(self, title):
        dialog = Adw.MessageDialog(transient_for=self.win, heading=title, modal=True)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('save', 'Save')
        dialog.set_close_response('cancel')
        dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
        return dialog

    @staticmethod
    def respond_to_form(dialog, callback):
        dialog.connect('response', lambda _dialog, response: callback() if response == 'save' else None)
        dialog.present()

    def message(self, title, text):
        dialog = Adw.MessageDialog(transient_for=self.win, heading=title, body=text, modal=True)
        dialog.add_response('ok', 'OK')
        dialog.present()

    def confirm(self, title, text, label, callback, *, transient=None):
        dialog = Adw.MessageDialog(transient_for=transient or self.win, heading=title, body=text, modal=True)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('apply', label)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _dialog, response: callback() if response == 'apply' else None)
        dialog.present()

    def install_menu(self, *_):
        path = DATA / 'applications' / f'{APP_ID}.desktop'
        try:
            write_launcher(path, 0o644)
        except (OSError, GLib.Error) as error:
            self.message('Could not install launcher', str(error))
            return
        self.message('Application launcher installed', str(path))

    def install_shortcut(self, *_):
        home = Path.home()
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        directory = Path(desktop) if desktop and Path(desktop) != home else home / 'Desktop'
        target = directory / f'{APP_ID}.desktop'
        try:
            write_launcher(target, 0o755)
        except (OSError, GLib.Error) as error:
            self.message('Could not install desktop shortcut', str(error))
            return
        message = str(target)
        try:
            Gio.File.new_for_path(str(target)).set_attribute_string(
                'metadata::trusted', 'true', Gio.FileQueryInfoFlags.NONE, None,
            )
        except GLib.Error:
            message += '\nIf prompted, right-click the shortcut and choose Allow Launching.'
        self.message('Desktop shortcut installed', message)

    def guide(self, *_):
        try:
            text = (SOURCE / 'USER-GUIDE.txt').read_text(encoding='utf-8')
        except OSError as error:
            self.message('Could not open the user guide', str(error))
            return
        guide = Gtk.Window(title=f'{APP_NAME} User Guide', transient_for=self.win, default_width=740,
                           default_height=620, modal=True, destroy_with_parent=True)
        view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            left_margin=24, right_margin=24, top_margin=20, bottom_margin=20)
        view.get_buffer().set_text(text)
        guide.set_child(Gtk.ScrolledWindow(child=view))
        guide.present()

    def about(self, *_):
        dialog = Adw.AboutWindow(
            transient_for=self.win, application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
            developer_name=AUTHOR, developers=[AUTHOR], comments='Schedule and share media channels across devices.',
        )
        dialog.present()


def serve():
    stopped = threading.Event()
    for name in ('SIGINT', 'SIGTERM'):
        signal.signal(getattr(signal, name), lambda *_: stopped.set())
    try:
        server = make_server(load_config())
        server.start()
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    try:
        print(f'Serving viewer links at {server.origin()}', flush=True)
        stopped.wait()
    finally:
        server.close()
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--install-launcher']:
        write_launcher(DATA / 'applications' / f'{APP_ID}.desktop', 0o644)
    elif sys.argv[1:] == ['--serve']:
        raise SystemExit(serve())
    else:
        raise SystemExit(ChannelTidemark().run())
