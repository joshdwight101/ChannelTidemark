#!/usr/bin/env bash
set -euo pipefail
source_dir="$(dirname "$(realpath "$0")")"
if [[ "${1:-}" != '--skip-deps' ]]; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg
fi
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
app_dir="$data_dir/channel-tidemark"
app_id=io.github.joshuadwight.ChannelTidemark
install -d "$app_dir/web" "$data_dir/applications"
install -m 755 "$source_dir/channel_tidemark.py" "$app_dir/"
install -m 755 "$source_dir/install-milkdrop.sh" "$source_dir/install-lactea.sh" "$app_dir/"
install -m 644 "$source_dir/stream_server.py" "$source_dir/media_links.py" "$source_dir/providers.py" "$source_dir/publishing.py" "$source_dir/milkdrop.py" "$source_dir/README.md" \
    "$source_dir/USER-GUIDE.txt" "$source_dir/changelog.md" "$app_dir/"
install -m 644 "$source_dir/web/"* "$app_dir/web/"
install -Dm644 "$source_dir/$app_id.svg" "$data_dir/icons/hicolor/scalable/apps/$app_id.svg"
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -ft "$data_dir/icons/hicolor"
fi
/usr/bin/python3 "$app_dir/channel_tidemark.py" --install-launcher
printf 'Channel Tidemark installed. Open it from your application menu.\n'
