#!/bin/sh
set -eu
presentation_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
runtime_node=/Users/caiyuheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
runtime_modules=/Users/caiyuheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
if [ ! -e "$presentation_dir/.build/node_modules" ]; then
  ln -s "$runtime_modules" "$presentation_dir/.build/node_modules"
fi
exec "$runtime_node" "$presentation_dir/.build/build_deck.mjs" "$@"
