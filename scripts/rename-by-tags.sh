#!/usr/bin/env bash
#
# Rename audio files to "Artist - Title.ext" using their embedded tags.
#
# Reads metadata with ffprobe (part of ffmpeg), so it works on files from any
# source, not just this project's downloads. Useful for the files the Python
# tool leaves alone because their match confidence was below the rename bar.
#
# By default it only SHOWS what it would do. Pass --apply to rename for real.
#
#   ./rename-by-tags.sh                 # preview this folder
#   ./rename-by-tags.sh --apply         # rename this folder
#   ./rename-by-tags.sh -r --apply      # include subfolders (album folders)
#   ./rename-by-tags.sh ~/Music --apply # a different folder
#
# Written for the bash 3.2 that ships with macOS.

set -u

AUDIO_EXTENSIONS="mp3 m4a flac wav aiff aif alac aac ogg opus wma"
MAX_STEM_CHARS=180

apply=0
recursive=0
target_dir=""

# Print the comment block at the top of this file as the help text.
usage() {
    awk 'NR >= 3 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -a|--apply)     apply=1 ;;
        -r|--recursive) recursive=1 ;;
        -h|--help)      usage 0 ;;
        -*)             printf 'Unknown option: %s\n\n' "$1" >&2; usage 1 ;;
        *)
            if [ -n "$target_dir" ]; then
                printf 'Only one folder can be given.\n' >&2
                exit 1
            fi
            target_dir="$1"
            ;;
    esac
    shift
done

if ! command -v ffprobe >/dev/null 2>&1; then
    cat >&2 <<'MSG'
ffprobe was not found. It comes with ffmpeg:

    brew install ffmpeg

(If you do not have Homebrew, see https://brew.sh)
MSG
    exit 1
fi

# Default to the folder this script lives in, which is the usual way to use
# it: drop a copy next to your music and run it.
if [ -z "$target_dir" ]; then
    target_dir="$(cd "$(dirname "$0")" && pwd)"
fi
if [ ! -d "$target_dir" ]; then
    printf 'Not a folder: %s\n' "$target_dir" >&2
    exit 1
fi

# Read one tag. stdin is redirected from /dev/null because ffprobe would
# otherwise swallow the file list this loop is reading.
# ffprobe matches the tag name case-insensitively but prints it
# with its original case (ARTIST= in FLAC, artist= in MP3), so match loosely.
# Tags usually live on the container; a few formats put them on the stream.
read_tag() {
    tag_file="$1"
    tag_name="$2"
    value="$(
        ffprobe -v error -show_entries "format_tags=$tag_name" \
            -of default=noprint_wrappers=1 -- "$tag_file" 2>/dev/null </dev/null |
            grep -i "^TAG:$tag_name=" | head -1 | sed "s/^[^=]*=//"
    )"
    if [ -z "$value" ]; then
        value="$(
            ffprobe -v error -show_entries "stream_tags=$tag_name" \
                -of default=noprint_wrappers=1 -- "$tag_file" 2>/dev/null </dev/null |
                grep -i "^TAG:$tag_name=" | head -1 | sed "s/^[^=]*=//"
        )"
    fi
    printf '%s' "$value"
}

# Strip characters that filenames cannot hold, tidy whitespace, and keep the
# result short enough for any filesystem.
sanitize() {
    printf '%s' "$1" |
        tr -d '\000-\037\177' |
        sed -e 's#[/\\:*?"<>|]#-#g' \
            -e 's/[[:space:]][[:space:]]*/ /g' \
            -e 's/^[[:space:].]*//' \
            -e 's/[[:space:].]*$//' |
        cut -c "1-$MAX_STEM_CHARS"
}

is_audio() {
    extension="$(printf '%s' "${1##*.}" | tr '[:upper:]' '[:lower:]')"
    for known in $AUDIO_EXTENSIONS; do
        [ "$extension" = "$known" ] && return 0
    done
    return 1
}

renamed=0
skipped_untagged=0
already_named=0
examined=0
planned_targets=""

# A name is taken if it exists on disk or if an earlier file in this run is
# already headed there. The second half matters for the preview, where
# nothing is actually created yet.
is_taken() {
    [ -e "$1" ] && return 0
    printf '%s\n' "$planned_targets" | grep -Fxq -- "$1"
}

if [ "$recursive" -eq 1 ]; then
    find_args="$target_dir"
else
    find_args="$target_dir -maxdepth 1"
fi

# -print0 so that spaces, quotes and newlines in filenames are handled.
while IFS= read -r -d '' file; do
    is_audio "$file" || continue
    examined=$((examined + 1))

    artist="$(read_tag "$file" artist)"
    title="$(read_tag "$file" title)"
    artist="$(sanitize "$artist")"
    title="$(sanitize "$title")"

    if [ -z "$title" ]; then
        printf 'no title tag, skipping:  %s\n' "$(basename "$file")"
        skipped_untagged=$((skipped_untagged + 1))
        continue
    fi

    # Keep the extension exactly as it is: changing only its case would be a
    # no-op rename that fails on case-insensitive disks.
    extension="${file##*.}"
    if [ -n "$artist" ]; then
        stem="$artist - $title"
    else
        stem="$title"          # tagged with a title but no artist
    fi

    directory="$(dirname "$file")"
    target="$directory/$stem.$extension"

    if [ "$target" = "$file" ]; then
        already_named=$((already_named + 1))
        continue
    fi

    # "Artist - Title (2).mp3" is already correctly named too: it is a second
    # copy that an earlier run numbered. Without this, every run would
    # renumber those files forever.
    current_stem="$(basename "$file")"
    current_stem="${current_stem%.*}"
    if [ "$(printf '%s' "$current_stem" | sed 's/ ([0-9][0-9]*)$//')" = "$stem" ]; then
        already_named=$((already_named + 1))
        continue
    fi

    # Never overwrite a different file that already has the target name.
    if is_taken "$target"; then
        suffix=2
        while is_taken "$directory/$stem ($suffix).$extension" && [ "$suffix" -lt 100 ]; do
            suffix=$((suffix + 1))
        done
        target="$directory/$stem ($suffix).$extension"
    fi
    planned_targets="$planned_targets
$target"

    if [ "$apply" -eq 1 ]; then
        if mv -n -- "$file" "$target"; then
            printf 'renamed: %s\n     ->  %s\n' "$(basename "$file")" "$(basename "$target")"
            renamed=$((renamed + 1))
        else
            printf 'FAILED:  %s\n' "$(basename "$file")" >&2
        fi
    else
        printf 'would rename: %s\n          ->  %s\n' "$(basename "$file")" "$(basename "$target")"
        renamed=$((renamed + 1))
    fi
done < <(find $find_args -type f -print0 2>/dev/null)

printf '\n%s audio file(s) examined in %s\n' "$examined" "$target_dir"
if [ "$apply" -eq 1 ]; then
    printf '%s renamed' "$renamed"
else
    printf '%s would be renamed' "$renamed"
fi
printf ', %s already correctly named, %s without usable tags\n' \
    "$already_named" "$skipped_untagged"

if [ "$apply" -eq 0 ] && [ "$renamed" -gt 0 ]; then
    printf '\nNothing has been changed. Re-run with --apply to rename.\n'
fi
