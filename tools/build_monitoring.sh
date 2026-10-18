#!/usr/bin/env bash
# Pinned pure-Go builds for both K3 and K1; no target libc or RVV dependency.
set -eo pipefail
. "$HOME/.x-cmd.root/X"
command -v go >/dev/null || x env use go
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_output="${1:-$task_root/artifacts/monitoring-binaries}"
mkdir -p "$task_output"
task_output="$(cd "$task_output" && pwd)"
export GOOS=linux GOARCH=riscv64 GORISCV64=rva20u64 CGO_ENABLED=0 GOTOOLCHAIN=go1.27.2
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
build_source() {
    local task_name="$1" task_tag="$2" task_commit="$3" task_repo="$HOME/.x-repo/github.com/$1"
    if [ ! -d "$task_repo/.git" ]; then
        mkdir -p "$(dirname "$task_repo")"
        git clone --depth 1 --branch "$task_tag" "https://github.com/$task_name.git" "$task_repo"
    fi
    [ "$(git -C "$task_repo" rev-parse HEAD)" = "$task_commit" ] || { echo "Source revision mismatch: $task_repo" >&2; exit 1; }
    git -C "$task_repo" diff --quiet HEAD
    cd "$task_repo"
}
build_source VictoriaMetrics/VictoriaMetrics v1.154.0 29c43cd2fee05409499a4d1b38e950e1b7f13bd4
go build -trimpath -ldflags '-s -w -X github.com/VictoriaMetrics/VictoriaMetrics/lib/buildinfo.Version=victoria-metrics-v1.154.0-riscv64-pure' -o "$task_output/victoria-metrics" ./app/victoria-metrics
build_source prometheus/node_exporter v1.10.2 654f19dee6a0c41de78a8d6d870e8c742cdb43b9
go build -trimpath -ldflags '-s -w -X github.com/prometheus/common/version.Version=1.10.2-riscv64-pure -X github.com/prometheus/common/version.Revision=654f19dee6a0c41de78a8d6d870e8c742cdb43b9' -o "$task_output/node_exporter" .
cd "$task_output"
sha256sum victoria-metrics node_exporter > SHA256SUMS
cat SHA256SUMS
