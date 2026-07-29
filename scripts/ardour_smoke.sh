#!/usr/bin/env bash
set -euo pipefail

bundle_input=${1:?usage: scripts/ardour_smoke.sh BUNDLE [SONG_ID]}
song_id=${2:-circuit_bloom}
expected_package='1:8.12.0+ds-1'
image='debian@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd'

if [[ ${SONGDNA_ARDOUR_SMOKE_INNER:-0} != 1 ]]; then
  command -v docker >/dev/null || { echo 'ardour smoke: docker is required' >&2; exit 2; }
  [[ -d $bundle_input ]] || { echo "ardour smoke: bundle not found: $bundle_input" >&2; exit 2; }
  bundle_abs=$(cd -- "$bundle_input" && pwd -P)
  repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
  if command -v songdna >/dev/null; then
    songdna handoff-validate "$bundle_abs" >/dev/null
  elif command -v python3.12 >/dev/null; then
    PYTHONPATH="$repo_root/src" python3.12 -m songdna.cli handoff-validate "$bundle_abs" >/dev/null
  else
    echo 'ardour smoke: install songdna or Python 3.12 to validate the bundle' >&2
    exit 2
  fi
  docker run --rm \
    -e SONGDNA_ARDOUR_SMOKE_INNER=1 \
    -v "$repo_root:/work:ro" \
    -v "$bundle_abs:/bundle:ro" \
    "$image" \
    bash -lc 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "ardour=1:8.12.0+ds-1" "ardour-data=1:8.12.0+ds-1" >/tmp/ardour-install.log && /work/scripts/ardour_smoke.sh /bundle "$1"' _ "$song_id"
  exit
fi

actual_package=$(dpkg-query -W -f='${Version}' ardour)
[[ $actual_package == "$expected_package" ]] || {
  echo "ardour smoke: package mismatch: expected $expected_package, got $actual_package" >&2
  exit 2
}
command -v ardour >/dev/null || { echo 'ardour smoke: ardour launcher is missing' >&2; exit 2; }
command -v ardour8-lua >/dev/null || { echo 'ardour smoke: ardour8-lua is missing' >&2; exit 2; }
command -v ardour8-new_session >/dev/null || { echo 'ardour smoke: ardour8-new_session is missing' >&2; exit 2; }
ardour_version_output=$(ardour --version 2>&1)
lua_version_output=$(ardour8-lua --version 2>&1)
ardour_version=''
lua_version=''
while IFS= read -r line; do
  [[ $line == *'Ardour8.12.0~ds'* ]] && ardour_version=$line
done <<< "$ardour_version_output"
while IFS= read -r line; do
  [[ $line == *'ardour-lua version 8.12.0~ds'* ]] && lua_version=$line
done <<< "$lua_version_output"
[[ -n $ardour_version ]] || { echo "ardour smoke: tool mismatch: $ardour_version_output" >&2; exit 2; }
[[ -n $lua_version ]] || { echo "ardour smoke: Lua tool mismatch: $lua_version_output" >&2; exit 2; }

smoke_root=$(mktemp -d /tmp/songdna_ardour_smoke_XXXXXX)
trap 'rm -rf -- "$smoke_root"' EXIT
session_dir="$smoke_root/$song_id"
start_seconds=$SECONDS
ardour8-new_session -s 48000 -m 2 "$session_dir" "$song_id"
ardour8-lua "$bundle_input/ardour-bootstrap.lua" "$session_dir" "$song_id"
ardour8-lua "$bundle_input/ardour-verify.lua" "$session_dir" "$song_id"

echo "SONGDNA_ARDOUR_VERSION $ardour_version"
echo "SONGDNA_ARDOUR_PACKAGE $actual_package"
echo "SONGDNA_ARDOUR_SAVE_REOPEN_SECONDS $((SECONDS - start_seconds))"
