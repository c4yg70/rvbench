# Deployment contract

Approved topology: manager and single-node VictoriaMetrics on 10.18.60.100;
workers on .100/.18/.167, native node exporters on each physical board.
Contract authority: rvbench-core-rust/observability.md and the service OpenAPI files.

Upgrade manager first; accept old registrations without observability; then workers.
Refuse to stop workers with unfinished jobs. Back up SQLite via its backup API before
service changes. Preserve service configs and binaries for rollback; retain legacy evald.
VictoriaMetrics listens only on loopback :8428, seven-day retention, 30s scrapes,
cache budget128MiB / cgroup768MiB, X100 CPUs0-1, one-core CPU quota, Nice10.
Exporters on .18/.167 use :9100 and general-purpose cores, low priority and 25% quota.
No authentication fields or admission list. No tests or fake measurements in services.
VM and exporter binaries must be supplied and fingerprinted, not fetched unversioned.
Existing .100 exporter remains in place; its legacy textfile data is excluded from VM.
