#!/usr/bin/env bash
# Build and run a local HydraDB node + indexer without Docker (Ubuntu 24.04 / WSL).
# This is the path Hydrant was developed and benchmarked on.
#   bash scripts/run_hydradb.sh [data_dir]
set -euo pipefail
DATA="${1:-$HOME/.hydrant/hydradb}"
SRC="${HYDRADB_SRC:-$HOME/.hydrant/src/hydradb}"
TOKEN="${HYDRADB_TOKEN:-local-development-token-32-bytes}"

if [ ! -x "$SRC/target/release/graph-node" ] || [ ! -x "$SRC/target/release/graph-indexer" ]; then
  sudo apt-get update
  sudo apt-get install -y build-essential clang libclang-dev cmake pkg-config libcypher-parser-dev libgraphblas-dev curl git
  command -v cargo >/dev/null || { curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; . "$HOME/.cargo/env"; }
  [ -d "$SRC" ] || git clone --depth 1 https://github.com/hydra-db/hydradb.git "$SRC"
  [ -f /opt/lagraph/lib/liblagraph.a ] || sudo bash "$SRC/scripts/install_lagraph.sh" /opt/lagraph
  (cd "$SRC" && LAGRAPH_LIB_DIR=/opt/lagraph/lib cargo build --release --locked \
      --features server-runtime,indexer-runtime --bin graph-node --bin graph-indexer)
fi

mkdir -p "$DATA/store" "$DATA/cache"
printf '%s\n' "$TOKEN" > "$DATA/auth-token"
export CLOUD_PROVIDER=local LOCAL_PATH="$DATA/store" GRAPH_DATA_PATH=data
export GRAPH_NAMESPACE=default GRAPH_ID=default GRAPH_CELL_ID=cell-0 GRAPH_CELLS=cell-0 GRAPH_NODE_ID=node-0
export GRAPH_BOLT_ADDR=127.0.0.1:7687 GRAPH_HTTP_ADDR=127.0.0.1:8443 GRAPH_ADMIN_ADDR=127.0.0.1:9090
export GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687 GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687
export GRAPH_INDEXER_ADMIN_ADDR=127.0.0.1:9092 GRAPH_INDEXER_NOTIFY_URL=http://127.0.0.1:9092/v1/changes:process
export GRAPH_DATA_CACHE_DIR="$DATA/cache" GRAPH_AUTH_TOKEN_FILE="$DATA/auth-token"
export GRAPH_ALLOW_PLAINTEXT=true RUST_MIN_STACK=33554432

"$SRC/target/release/graph-indexer" > "$DATA/indexer.log" 2>&1 &
trap 'kill %1 2>/dev/null || true' EXIT
echo "HydraDB: bolt://127.0.0.1:7687  http://127.0.0.1:8443  (logs in $DATA)"
exec "$SRC/target/release/graph-node"
