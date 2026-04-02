#!/bin/bash
# VideoToolbox ffmpeg wrapper for Immich Accelerator
#
# Placed earlier in PATH than the real ffmpeg. Intercepts encoding calls
# and adds VideoToolbox hardware acceleration flags. Non-encoding calls
# (probing, frame extraction) pass through unchanged.
#
# This is needed because Immich only supports nvenc/qsv/vaapi/rkmpp as
# accel options — no videotoolbox. Instead of patching Immich, we wrap
# the ffmpeg binary.

REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"

# Check if this is an encoding call (has -c:v or -vcodec with a software encoder)
ARGS=("$@")
USE_HW=false
NEW_ARGS=()

for ((i=0; i<${#ARGS[@]}; i++)); do
    arg="${ARGS[$i]}"
    next="${ARGS[$((i+1))]:-}"

    # Remap software encoders to VideoToolbox hardware encoders
    if [[ "$arg" == "-c:v" || "$arg" == "-vcodec" ]]; then
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
                continue
                ;;
        esac
    fi

    # Remove -preset (VideoToolbox doesn't support CPU presets)
    if [[ "$arg" == "-preset" && "$USE_HW" == true ]]; then
        ((i++))  # skip the preset value too
        continue
    fi

    NEW_ARGS+=("$arg")
done

# Add hardware decode if we're doing hardware encode
if [[ "$USE_HW" == true ]]; then
    exec "$REAL_FFMPEG" -hwaccel videotoolbox "${NEW_ARGS[@]}"
else
    exec "$REAL_FFMPEG" "${NEW_ARGS[@]}"
fi
