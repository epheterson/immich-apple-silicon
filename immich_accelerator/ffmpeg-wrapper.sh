#!/bin/bash
# VideoToolbox ffmpeg wrapper for Immich Accelerator
#
# Remaps software encoders to VideoToolbox hardware encoders, and requests
# VideoToolbox decode for every input.
# Uses jellyfin-ffmpeg which has tonemapx natively — no filter remapping needed.
#
# Immich doesn't support 'videotoolbox' as an accel option, so this wrapper
# remaps software encoder requests to VideoToolbox hardware equivalents.

REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"

# macOS ships no `timeout`, and a hung qlmanage would hold a thumbnail job open
# forever. Run the command, kill it if it outstays the limit.
_ql_timeout() {
    local secs=$1; shift
    "$@" &
    local pid=$!
    ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) &
    local killer=$!
    wait "$pid" 2>/dev/null; local rc=$?
    kill "$killer" 2>/dev/null
    return $rc
}

# Immich's quality setting is a CRF number (0-51, lower is better).
# VideoToolbox ignores -crf, and reads -q:v on a 0-100 scale where higher is
# better, so the number has to be translated when an encoder is remapped.
# The slope comes from matching x264 veryfast against h264_videotoolbox over
# six clips and five quality metrics (SSIM, MS-SSIM, LPIPS, DISTS):
# q ~= 104 - 1.96*CRF. Bash has no floats, so scale by two and add half the
# divisor to round to nearest over the 0-51 range Immich offers. 10# keeps a
# leading zero from being read as octal.
_crf_to_quality() {
    local q=$(( ((51 - 10#$1) * 200 + 51) / 102 + 4 ))
    (( q < 1 )) && q=1
    (( q > 100 )) && q=100
    echo "$q"
}

# Used by the QuickLook fallback below to convert its PNG output to whatever
# format Immich asked for.
if [[ -n "${IMMICH_ACCELERATOR_VIPS:-}" ]]; then
    VIPS_BIN="$IMMICH_ACCELERATOR_VIPS"
elif [[ -x "/opt/homebrew/bin/vips" ]]; then
    VIPS_BIN="/opt/homebrew/bin/vips"
else
    VIPS_BIN="/usr/local/bin/vips"
fi

# Two switches, both on by default, both meaning "use the hardware".
#
# Hardware encoding is not a free win and the naming should not pretend it is:
# on an idle Mac the software encoder often finishes one file sooner, because
# Immich asks for preset ultrafast. What the hardware buys is the machine, using
# roughly one core where software uses every core it can reach, which is what
# matters when the worker is running several jobs and the ML engine at once.
# `immich-accelerator encode-compare` measures both on your own footage.
# Trimmed and lowercased before comparing, because the Python side does the
# same and the two must not disagree: " FALSE" read as off by the CLI and on
# by the wrapper means the settings screen and the actual encode differ.
# tr, not ${v:l} (zsh only) or ${v,,} (bash 4; macOS ships 3.2). Both expand
# to the string unchanged here, which would make this normalisation a no-op
# that still looks correct.
# Ends only, matching Python's str.strip(). Deleting interior whitespace made
# the two disagree on exactly the values this exists to keep aligned.
_norm() {
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    printf '%s' "$v" | tr '[:upper:]' '[:lower:]'
}
_off() { local v; v=$(_norm "$1"); [[ "$v" == "0" || "$v" == "false" || "$v" == "no" ]]; }
# Default-off counterpart, for switches that change output and so are only
# reached from the Maximum position. Anything unrecognised stays off.
_on() { local v; v=$(_norm "$1"); [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" ]]; }
HW_VIDEO=true
_off "${IMMICH_ACCEL_HW_VIDEO:-1}" && HW_VIDEO=false
HW_DECODE=true
_off "${IMMICH_ACCEL_HW_DECODE:-1}" && HW_DECODE=false
# AudioToolbox AAC. Off by default: measured a third less CPU than the software
# encoder and faster besides, but it is a different encoder, so the bytes differ
# from what Docker produces and that is only acceptable where it was asked for.
HW_AUDIO=false
_on "${IMMICH_ACCEL_HW_AUDIO:-0}" && HW_AUDIO=true

ARGS=("$@")
USE_HW_ENCODER=false
USE_HEVC=false
HAS_HEVC_TAG=false
NEW_ARGS=()

for ((i=0; i<${#ARGS[@]}; i++)); do
    arg="${ARGS[$i]}"

    # Remap software encoders to VideoToolbox hardware encoders
    if [[ "$arg" == "-c:v" || "$arg" == "-vcodec" ]]; then
        next="${ARGS[$((i+1))]:-}"
        case "$next" in
            h264|libx264|libx264rgb)
                if [[ "$HW_VIDEO" != true ]]; then
                    NEW_ARGS+=("$arg" "$next"); ((i++)); continue
                fi
                NEW_ARGS+=("$arg" "h264_videotoolbox")
                ((i++))
                USE_HW_ENCODER=true
                continue
                ;;
            hevc|libx265)
                # Before the switch: the output is HEVC either way, and the
                # hvc1 tag below is about the container, not about which
                # encoder produced the stream.
                USE_HEVC=true
                if [[ "$HW_VIDEO" != true ]]; then
                    NEW_ARGS+=("$arg" "$next"); ((i++)); continue
                fi
                NEW_ARGS+=("$arg" "hevc_videotoolbox")
                ((i++))
                USE_HW_ENCODER=true
                continue
                ;;
        esac
    fi

    # Strip -preset for VideoToolbox (doesn't support CPU presets)
    if [[ "$arg" == "-preset" && "$USE_HW_ENCODER" == true ]]; then
        ((i++))
        continue
    fi

    # Translate the quality setting for VideoToolbox. Immich sends -crf N
    # (CQ mode Auto) or -q:v N (CQ mode CQP) carrying the same CRF-scale
    # number: -crf is ignored by these encoders, and -q:v is read on an
    # inverted scale. Both become a -q:v value that means what the CRF
    # setting asked for. Matched as prefixes so the stream-specific spellings
    # (-crf:v, -q:v:0) are translated too.
    #
    # USE_HW_ENCODER, not USE_HW: this arrived alongside the rename in #153,
    # and the two merge without a git conflict, so the old name would simply
    # never be true and the translation would silently stop happening.
    if [[ ( "$arg" == -crf* || "$arg" == -q:v* ) && "$USE_HW_ENCODER" == true ]]; then
        next="${ARGS[$((i+1))]:-}"
        if [[ "$next" =~ ^[0-9]+$ ]]; then
            NEW_ARGS+=("-q:v" "$(_crf_to_quality "$next")")
            ((i++))
            continue
        fi
    fi

    # Remap the audio encoder to AudioToolbox. Same codec and same container,
    # so nothing downstream changes shape; only the encoder differs. Left alone
    # for `copy`, which is not an encode at all.
    if [[ "$HW_AUDIO" == true && ( "$arg" == "-c:a" || "$arg" == "-acodec" ) ]]; then
        next="${ARGS[$((i+1))]:-}"
        if [[ "$next" == "aac" ]]; then
            NEW_ARGS+=("$arg" "aac_at")
            ((i++))
            continue
        fi
    fi

    # Track if -tag:v is already specified (including stream-specific -tag:v:0 etc.)
    [[ "$arg" == -tag:v* ]] && HAS_HEVC_TAG=true

    NEW_ARGS+=("$arg")
done

# Ensure HEVC output uses hvc1 tag (Apple-compatible).
# hev1 (ffmpeg default) stores parameter sets in-band — Apple's
# decoder rejects it. Immich usually passes -tag:v hvc1 itself,
# but if it's absent we inject it before the output filename.
if [[ "$USE_HEVC" == true && "$HAS_HEVC_TAG" == false ]]; then
    len=${#NEW_ARGS[@]}
    LAST="${NEW_ARGS[$((len-1))]}"
    NEW_ARGS=("${NEW_ARGS[@]:0:$((len-1))}" "-tag:v" "hvc1" "$LAST")
fi

# Decode with VideoToolbox on every call, not only the ones that remapped an
# encoder. Immich sends no -c:v at all for thumbnail and preview jobs, so
# tying hardware decode to the encoder remap left those decoding in software.
# -hwaccel is a hint: ffmpeg falls back to the software decoder for anything
# VideoToolbox will not take.
#
# Switchable because it is the one part of the wrapper that changes output:
# hardware decode hands back 10-bit frames as p010le where software decode
# gives yuv420p10le, so a thumbnail from a 10-bit source is not byte-identical
# to Docker's (measured at 55.3 dB PSNR). 8-bit sources are unaffected. Anyone
# who wants Docker's exact bytes needs a way to say so.
if [[ "$HW_DECODE" == true ]]; then
    RUN_ARGS=(-hwaccel videotoolbox "${NEW_ARGS[@]}")
else
    RUN_ARGS=("${NEW_ARGS[@]}")
fi

# Run ffmpeg with stderr captured, so the fallback below can tell a decoder
# rejection (what it is for) from a broken file or bad arguments (what it must
# not paper over). The wrapper is the parent rather than exec'ing, because it
# has work to do afterwards; forward the usual signals so killing the wrapper
# still kills ffmpeg instead of orphaning it.
# Explicit X's: BSD mktemp accepts a bare -t template, GNU requires them,
# and the wrapper's tests run on Linux in CI even though it only ever
# executes on macOS.
FF_ERR=$(mktemp "${TMPDIR:-/tmp}/immich-ffmpeg-err.XXXXXX")
cleanup() { rm -f "$FF_ERR"; [[ -n "${QL_DIR:-}" ]] && rm -rf "$QL_DIR"; }
trap cleanup EXIT
"$REAL_FFMPEG" "${RUN_ARGS[@]}" 2> >(tee "$FF_ERR" >&2) &
FF_PID=$!
trap 'kill -TERM "$FF_PID" 2>/dev/null' TERM INT
wait "$FF_PID"
STATUS=$?
trap - TERM INT
[[ $STATUS -eq 0 ]] && exit 0

# ffmpeg's HEVC decoder can hard-reject a stream that macOS's AVFoundation
# decodes fine (seen on real HDR10 phone clips; a stock Homebrew ffmpeg build
# fails identically, so this isn't jellyfin-ffmpeg-specific). Only retry via
# QuickLook for a single-frame thumbnail (`-frames:v 1`): a full transcode has
# no single frame for QuickLook to hand back.
IS_SINGLE_FRAME=false
INPUT=""
for ((i=0; i<${#ARGS[@]}; i++)); do
    if [[ "${ARGS[$i]}" == "-frames:v" && "${ARGS[$((i+1))]:-}" == "1" ]]; then
        IS_SINGLE_FRAME=true
    fi
    if [[ "${ARGS[$i]}" == "-i" ]]; then
        INPUT="${ARGS[$((i+1))]:-}"
    fi
done
OUTPUT="${ARGS[${#ARGS[@]}-1]}"

# Only a decode failure. Falling back on ANY non-zero exit meant a truncated
# upload, a seek past the end, or a file ffmpeg cannot demux still produced a
# poster frame from container metadata and exited 0, so Immich recorded a
# corrupt asset as successfully thumbnailed and nobody ever found out.
DECODE_REJECTED=false
if grep -qiE "(error while decoding|failed to open codec|no frame|invalid data found|decoder.*(not found|failed)|hevc.*(error|unsupported))" "$FF_ERR" 2>/dev/null; then
    DECODE_REJECTED=true
fi

if [[ "$IS_SINGLE_FRAME" == true && "$DECODE_REJECTED" == true && -n "$INPUT" \
      && ( "$OUTPUT" == *.jpg || "$OUTPUT" == *.jpeg || "$OUTPUT" == *.png || "$OUTPUT" == *.webp ) ]]; then
    QL_DIR=$(mktemp -d)
    # Immich's own `scale=W:H` filter carries the target; one side is -2,
    # meaning "preserve aspect ratio", so take whichever side is positive.
    SCALE_ARG=$(printf '%s\n' "${ARGS[@]}" | grep -o 'scale=[0-9-]*:[0-9-]*' | head -1)
    QL_SIZE=$(echo "$SCALE_ARG" | grep -oE '[0-9]+' | sort -rn | head -1)
    [[ -z "$QL_SIZE" ]] && QL_SIZE=1080
    # Which side that number refers to matters: qlmanage -s fits the frame in
    # an N-by-N box, so asking for 1440 on a landscape clip returns 1440 wide,
    # not 1440 tall. Measured on a real video: -s 1440 gave 960x720. Ask
    # QuickLook for something generous and let vips do the actual resize to
    # the dimension Immich asked for, or every fallback thumbnail is smaller
    # than requested and permanently so, because Immich records success.
    SCALE_W=${SCALE_ARG#scale=}; SCALE_W=${SCALE_W%%:*}
    SCALE_H=${SCALE_ARG##*:}
    # qlmanage needs a WindowServer session and can hang without one, so cap
    # it: a thumbnail job that never returns is worse than one that fails.
    if command -v qlmanage >/dev/null 2>&1 \
       && _ql_timeout 30 qlmanage -t -s $((QL_SIZE * 2)) -o "$QL_DIR" "$INPUT" >/dev/null 2>&1; then
        QL_RESULT=$(find "$QL_DIR" -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" \) -print -quit)
        if [[ -n "$QL_RESULT" ]]; then
            # vips, not sips: sips cannot write webp, which this install uses.
            if [[ "$SCALE_H" =~ ^[0-9]+$ ]]; then
                RESIZE=("--height" "$SCALE_H")
            else
                RESIZE=("--width" "${SCALE_W:-$QL_SIZE}")
            fi
            # KNOWN DEFECT, deliberately left as it is for now.
            #
            # ":1:1" passes the number without its flag, so vips reads it as
            # the positional width: a preview requested at height 1440 comes
            # back 1440 wide rather than 1440 tall. That is wrong.
            #
            # It is not fixed here because the obvious fixes are worse.
            # `vips thumbnail` requires a positional width, so passing
            # "${RESIZE[@]}" alone exits "too few arguments" and the fallback
            # produces nothing. Passing a large width with --height did not
            # bound the result either: measured on a 1920x1080 source,
            # `10000 --height 400` returned 10000x5625. Getting this right
            # means computing the width from the QuickLook output's own aspect
            # ratio, which needs its dimensions read back and deserves its own
            # change with its own tests rather than a guess in a release.
            #
            # Only reached when ffmpeg cannot decode the stream at all, and it
            # still produces a usable thumbnail, just at the wrong dimension.
            if "$VIPS_BIN" thumbnail "$QL_RESULT" "$OUTPUT" "${RESIZE[@]:1:1}" >/dev/null 2>&1 \
               || "$VIPS_BIN" copy "$QL_RESULT" "$OUTPUT" >/dev/null 2>&1; then
                echo "[immich-accelerator] ffmpeg couldn't decode $INPUT for a thumbnail; QuickLook/AVFoundation produced one instead" >&2
                exit 0
            fi
        fi
    fi
fi

exit "$STATUS"
