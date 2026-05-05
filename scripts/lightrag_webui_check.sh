#!/bin/bash
# Check if lightrag_webui is reachable at macmini.local:9622
set -euo pipefail

echo "Checking lightrag_webui at http://macmini.local:9622..."
curl -fsS http://macmini.local:9622 -o /dev/null