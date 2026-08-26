#!/bin/bash
# NiccoPDF launcher for macOS — double-click me.
# (If macOS refuses to open it: right-click -> Open, or run once in Terminal:
#   chmod +x NiccoPDF.command && xattr -c NiccoPDF.command )
DIR="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$DIR/macos/nicco_launch.sh" "$DIR" "$@"
