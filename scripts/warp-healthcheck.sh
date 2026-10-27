#!/bin/sh
set -eu

# Check the actual HTTP proxy path, including WARP egress. Do not let an
# inherited NO_PROXY setting bypass it or accept a partially failed curl.
trace=$(curl --noproxy '' --proxy http://127.0.0.1:1080 \
    --fail --silent --show-error --max-time 8 \
    https://cloudflare.com/cdn-cgi/trace)
printf '%s\n' "$trace" | grep -Eq '^warp=(on|plus)$'
