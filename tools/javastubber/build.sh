#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "${SCRIPT_DIR}"
mvn clean package -q -B
echo "Built javastubber.jar -> target/javastubber-1.0-SNAPSHOT.jar"
