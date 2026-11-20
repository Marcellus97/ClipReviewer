#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python="$script_dir/.venv/bin/python"

if [ ! -x "$python" ]; then
    echo "Anki environment not found. Run:" >&2
    echo "  python3.13 -m venv $script_dir/.venv" >&2
    echo "  $script_dir/.venv/bin/python -m pip install -r $script_dir/requirements-anki.txt" >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    set -- "$script_dir/clips"
fi

exec "$python" "$script_dir/anki_exporter.py" "$@"
