#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != --skip-deps ]]; then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends build-essential cmake curl libgl1-mesa-dev mesa-common-dev libegl1 python3-av
fi
version=4.1.8
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/channel-tidemark"
install -d "$data_dir"
work_dir="$(mktemp -d "$data_dir/milkdrop-build.XXXXXXXX")"
trap 'rm -r -- "$work_dir"' EXIT
archive="$work_dir/projectm.tar.gz"
curl -fL --retry 2 "https://github.com/projectM-visualizer/projectm/releases/download/v$version/libprojectM-$version.tar.gz" -o "$archive"
printf '%s  %s\n' f72135e8b8e5fd807f908537255b41ac7ba4830309265e830cb756535487239e "$archive" | sha256sum --check -
tar -xzf "$archive" -C "$work_dir"
cmake -S "$work_dir/libprojectM-$version" -B "$work_dir/build" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$data_dir/milkdrop-engine" -DCMAKE_INSTALL_LIBDIR=lib \
    -DENABLE_SYSTEM_PROJECTM_EVAL=OFF -DENABLE_PLAYLIST=OFF
cmake --build "$work_dir/build" --parallel 2
cmake --install "$work_dir/build"
printf 'projectM %s installed in %s\n' "$version" "$data_dir/milkdrop-engine"
