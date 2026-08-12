#!/bin/bash
# VideoToolbox ffmpeg wrapper for Immich Accelerator
#
# Remaps software encoders to VideoToolbox hardware encoders.
# Uses jellyfin-ffmpeg which has tonemapx natively — no filter remapping needed.
#
# Immich doesn't support 'videotoolbox' as an accel option, so this wrapper
# remaps software encoder requests to VideoToolbox hardware equivalents.

REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"

# Used by the QuickLook fallback below to convert its PNG output to whatever
# format Immich asked for.
if [[ -n "${IMMICH_ACCELERATOR_VIPS:-}" ]]; then
    VIPS_BIN="$IMMICH_ACCELERATOR_VIPS"
elif [[ -x "/opt/homebrew/bin/vips" ]]; then
    VIPS_BIN="/opt/homebrew/bin/vips"
else
    VIPS_BIN="/usr/local/bin/vips"
fi

ARGS=("$@")
USE_HW=false
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
                NEW_ARGS+=("$arg" "h264_videotoolbox")
                ((i++))
                USE_HW=true
                continue
                ;;
            hevc|libx265)
                NEW_ARGS+=("$arg" "hevc_videotoolbox")
                ((i++))
                USE_HW=true
                USE_HEVC=true
                continue
                ;;
        esac
    fi

    # Strip -preset for VideoToolbox (doesn't support CPU presets)
    if [[ "$arg" == "-preset" && "$USE_HW" == true ]]; then
        ((i++))
        continue
    fi

    # Track if -tag:v is already specified (including stream-specific -tag:v:0 etc.)
    [[ "$arg" == -tag:v* ]] && HAS_HEVC_TAG=true

    NEW_ARGS+=("$arg")
done

if [[ "$USE_HW" == true ]]; then
    # Ensure HEVC output uses hvc1 tag (Apple-compatible).
    # hev1 (ffmpeg default) stores parameter sets in-band — Apple's
    # decoder rejects it. Immich usually passes -tag:v hvc1 itself,
    # but if it's absent we inject it before the output filename.
    if [[ "$USE_HEVC" == true && "$HAS_HEVC_TAG" == false ]]; then
        len=${#NEW_ARGS[@]}
        LAST="${NEW_ARGS[$((len-1))]}"
        NEW_ARGS=("${NEW_ARGS[@]:0:$((len-1))}" "-tag:v" "hvc1" "$LAST")
    fi
    RUN_ARGS=(-hwaccel videotoolbox "${NEW_ARGS[@]}")
else
    RUN_ARGS=("${NEW_ARGS[@]}")
fi

"$REAL_FFMPEG" "${RUN_ARGS[@]}"
STATUS=$?
[[ $STATUS -eq 0 ]] && exit 0

# ffmpeg's HEVC decoder can hard-reject a stream that macOS's AVFoundation
# decodes fine (seen on real HDR10 phone clips; a stock Homebrew ffmpeg build
# fails identically, so this isn't jellyfin-ffmpeg-specific). Only retry via
# QuickLook for a single-frame thumbnail (`-frames:v 1`) — a full transcode
# has no single frame for QuickLook to hand back.
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

if [[ "$IS_SINGLE_FRAME" == true && -n "$INPUT" && ( "$OUTPUT" == *.jpg || "$OUTPUT" == *.jpeg || "$OUTPUT" == *.png || "$OUTPUT" == *.webp ) ]]; then
    QL_DIR=$(mktemp -d)
    # Target pixel size lives in Immich's own `scale=W:H` filter (one side is
    # -2, meaning "preserve aspect ratio"); take whichever side is positive.
    SCALE_ARG=$(printf '%s\n' "${ARGS[@]}" | grep -o 'scale=[0-9-]*:[0-9-]*' | head -1)
    QL_SIZE=$(echo "$SCALE_ARG" | grep -oE '[0-9]+' | sort -rn | head -1)
    [[ -z "$QL_SIZE" ]] && QL_SIZE=1080
    if qlmanage -t -s "$QL_SIZE" -o "$QL_DIR" "$INPUT" >/dev/null 2>&1; then
        QL_RESULT=$(find "$QL_DIR" -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" \) -print -quit)
        # vips, not sips: sips can't write webp, which this install uses for thumbnails.
        if [[ -n "$QL_RESULT" ]] && "$VIPS_BIN" copy "$QL_RESULT" "$OUTPUT" >/dev/null 2>&1; then
            echo "[immich-accelerator] ffmpeg couldn't decode $INPUT for a thumbnail; QuickLook/AVFoundation produced one instead" >&2
            rm -rf "$QL_DIR"
            exit 0
        fi
    fi
    rm -rf "$QL_DIR"
fi

exit "$STATUS"
