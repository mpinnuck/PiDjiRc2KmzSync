#!/bin/sh

set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
archive_name="PiDjiRc2KmzSync-source.zip"

cd "$project_root"

rm -f "$archive_name"

zip -r "$archive_name" README.md code mechanical \
    -x '*.venv/*' \
       '*/.venv/*' \
       '*/__pycache__/*' \
       '*.pyc' \
       '*.pyo' \
       '*.pyd' \
       '.DS_Store' \
       '*/.DS_Store' \
       '*.zip' \
       'code/kmz_sync_config.json'

printf 'Created %s\n' "$archive_name"