#!/usr/bin/env bash
set -euo pipefail

install_bailout() {
  local platform arch asset version base tmp expected actual dir resolved
  for dependency in curl tar bash; do
    command -v "$dependency" >/dev/null || { echo "bailout: $dependency is required." >&2; return 1; }
  done
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) platform=macos; arch=arm64 ;;
    Linux/x86_64|Linux/amd64) platform=linux; arch=x64 ;;
    Linux/aarch64|Linux/arm64) platform=linux; arch=arm64 ;;
    *) echo 'bailout supports Apple Silicon macOS and x64/arm64 Linux.' >&2; return 1 ;;
  esac
  base=https://github.com/storozhenko98/bailout/releases
  version=${BAILOUT_VERSION:-}
  if [ -z "$version" ]; then
    resolved=$(curl -q -fsSLI --connect-timeout 15 --max-time 60 -o /dev/null -w '%{url_effective}' "$base/latest")
    version=${resolved##*/}
  fi
  if [[ ! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'bailout: cannot resolve a stable release.' >&2; return 1
  fi
  asset="bailout-${platform}-${arch}.tar.gz"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  echo "Installing bailout $version ($platform/$arch)…"
  curl -q -fsSL --connect-timeout 15 --max-time 120 "$base/download/$version/$asset" -o "$tmp/$asset"
  curl -q -fsSL --connect-timeout 15 --max-time 60 "$base/download/$version/SHA256SUMS" -o "$tmp/SHA256SUMS"
  expected=$(awk -v asset="$asset" '$2 == asset {print $1}' "$tmp/SHA256SUMS")
  if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then echo 'Missing or invalid release checksum.' >&2; return 1; fi
  if command -v sha256sum >/dev/null; then actual=$(sha256sum "$tmp/$asset");
  elif command -v shasum >/dev/null; then actual=$(shasum -a 256 "$tmp/$asset");
  else echo 'sha256sum or shasum is required.' >&2; return 1; fi
  actual=${actual%% *}
  if [ "$expected" != "$actual" ]; then echo 'Checksum mismatch. Nothing installed.' >&2; return 1; fi
  if [ "$(tar -tzf "$tmp/$asset")" != bailout ]; then echo 'Unexpected archive contents.' >&2; return 1; fi
  tar -xzf "$tmp/$asset" -C "$tmp"
  if [ ! -f "$tmp/bailout" ] || [ -L "$tmp/bailout" ] || [ "$(wc -c < "$tmp/bailout")" -ge 6000000 ]; then
    echo 'Invalid binary or binary exceeds the 6 MB size limit.' >&2; return 1
  fi
  dir=${BAILOUT_INSTALL_DIR:-}
  if [ -z "$dir" ]; then
    case ":$PATH:" in
      *":$HOME/.local/bin:"*) dir="$HOME/.local/bin" ;;
      *)
        for dir in /usr/local/bin /opt/homebrew/bin; do
          if [ -d "$dir" ] && [ -w "$dir" ]; then break; fi
          dir=
        done
        dir=${dir:-"$HOME/.local/bin"}
        ;;
    esac
  fi
  mkdir -p "$dir"
  # Move a complete file into place, preserving the existing binary if download fails.
  local staged
  staged=$(mktemp "$dir/.bailout.XXXXXX")
  if ! cp "$tmp/bailout" "$staged" || ! chmod 755 "$staged" || ! mv -f "$staged" "$dir/bailout"; then
    rm -f "$staged"; return 1
  fi
  echo "Installed $dir/bailout ($(wc -c < "$dir/bailout" | tr -d ' ') bytes)."
  case ":$PATH:" in
    *":$dir:"*) echo 'Run: bailout' ;;
    *)
      echo "Add this directory to your shell PATH, then run bailout:"
      printf '  export PATH=%q:"$PATH"\n' "$dir"
      ;;
  esac
  rm -rf "$tmp"
  trap - EXIT
}
install_bailout
