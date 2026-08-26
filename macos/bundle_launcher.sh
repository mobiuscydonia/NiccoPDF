#!/bin/bash
# Contents/MacOS/NiccoPDF — entry point of the NiccoPDF.app bundle.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
exec /bin/bash "$RES/nicco_launch.sh" "$RES" "$@"
