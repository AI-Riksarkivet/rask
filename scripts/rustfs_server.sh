#!/usr/bin/env bash
# rustfs_server.sh: print the path of the pinned RustFS server binary, fetching it into .localbin/ when absent.
#
# The Rust outbox's store tests (ADR 0234) run this release as a subprocess: 1.0.3, the version the rustfs fixture and the
# Dagger lanes run (.dagger/storage.go), downloaded only at the digest scripts/toolchain.sha256 pins for its URL, the
# release's own SHA256SUMS.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
version="1.0.3"
binary="$root/.localbin/rustfs-$version"
if [ ! -x "$binary" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) asset="rustfs-linux-x86_64-musl-v$version.zip" ;;
    Linux-aarch64 | Linux-arm64) asset="rustfs-linux-aarch64-musl-v$version.zip" ;;
    Darwin-arm64) asset="rustfs-macos-aarch64-v$version.zip" ;;
    *) echo "!! no pinned RustFS for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
  esac
  mkdir -p "$root/.localbin"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  bash "$root/scripts/fetch_pinned.sh" "https://github.com/rustfs/rustfs/releases/download/$version/$asset" "$work/rustfs.zip"
  unzip -q "$work/rustfs.zip" -d "$work/unpacked"
  mv "$(find "$work/unpacked" -type f -name rustfs | head -1)" "$binary"
  chmod +x "$binary"
fi
echo "$binary"
