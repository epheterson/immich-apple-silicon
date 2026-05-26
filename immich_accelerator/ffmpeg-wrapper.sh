#!/bin/bash
# VideoToolbox ffmpeg wrapper for Immich Accelerator
#
# Remaps software encoders to VideoToolbox hardware encoders.
# Uses jellyfin-ffmpeg which has tonemapx natively — no filter remapping needed.
#
# Immich doesn't support 'videotoolbox' as an accel option, so this wrapper
# remaps software encoder requests to VideoToolbox hardware equivalents.

REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"

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
    exec "$REAL_FFMPEG" -hwaccel videotoolbox "${NEW_ARGS[@]}"
else
    exec "$REAL_FFMPEG" "${NEW_ARGS[@]}"
fi
