#!/usr/bin/env bash
set -euo pipefail
if [[ "$(uname -m)" != x86_64 ]]; then
    printf 'The bundled Lactea installer supports x86-64 Linux.\n' >&2
    exit 1
fi
source_dir="$(dirname "$(realpath "$0")")"
version=v0.2.2
release="lactea-$version-linux-x86_64"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/channel-tidemark"
target="$data_dir/lactea-engine"
install -d -m 700 "$target"
work_dir="$(mktemp -d "$target/install.XXXXXXXX")"
trap 'rm -r -- "$work_dir"' EXIT
archive="$work_dir/$release.tar.gz"
curl -fL --retry 2 "https://github.com/M0Rf30/lactea/releases/download/$version/$release.tar.gz" -o "$archive"
printf '%s  %s\n' 2c2208cace8d081c80f34628aaab3c0391cb79986da2e2ec4fe869fd0a765f2f "$archive" | sha256sum --check -
tar -xzf "$archive" -C "$work_dir" "$release/lib/liblactea_capi.so" \
    "$release/LICENSE-MIT" "$release/LICENSE-APACHE" "$release/THIRD_PARTY_NOTICES.md"
library="$work_dir/$release/lib/liblactea_capi.so"
TIDEMARK_LACTEA_LIBRARY="$library" /usr/bin/python3 "$source_dir/milkdrop.py" --engine lactea --check
install -m 644 "$work_dir/$release/"{LICENSE-MIT,LICENSE-APACHE,THIRD_PARTY_NOTICES.md} "$target/"
mv -f -- "$library" "$target/liblactea_capi.so"
printf 'Lactea %s installed and its off-screen render check passed.\n' "$version"
