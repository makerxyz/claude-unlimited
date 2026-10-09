# Gateway stalls and safe upgrades

The macOS gateway serves interactive inference and dashboard requests. Its
LaunchAgent must use `ProcessType=Interactive`; `Background` can starve local
health checks when the host is busy. The installer sets this classification,
and the upgrade preflight rejects source and built packages that restore
Background. A service still being present in launchd is not proof it responds.

`scripts/upgrade-when-idle.sh` waits up to 1,200 seconds for one healthy
`/api/status` snapshot to confirm no requests are serving and at least ten
seconds of idle time. `CU_IDLE_MAX_WAIT` changes the cap; zero means one check.
A timeout, missing fields, or a busy pool cannot authorize a swap. At the cap,
the script aborts and leaves the live app and venv unchanged. For an explicitly
authorized outage repair, `CU_FORCE_RESTART=1` bypasses the idle guard and logs
that active requests may be interrupted. Do not replay their prompts: the
upstream may already have consumed them.

The gateway counts overlapping requests per profile. Completing one response
does not make its other streams idle. The five-minute dashboard display cap
also cannot turn an open response into an idle signal for an update. A hung
request can therefore defer an automatic update until investigated; preserving
its unknown consumption state is safer than inferring completion from age.

Diagnose local responsiveness separately from upstream generation: read
`/health`, `/api/profiles`, and `/v1/models`; inspect service classification,
recent completion metadata, and host pressure. Upstream socket timeouts and SSE
keepalives already bound silent waits without blocking the local health path.
An idle/status read that fails does not mean there are no upstream requests.
The upgrade log records the source commit, swap, verification, and rollback;
matching package version strings alone cannot distinguish builds.

Installer and upgrade-guard tests are offline. The blocked-upstream regression
uses four fake inference calls on an ephemeral loopback server and confirms
that health and models still respond. No production prompt is needed to test
these failure modes. A scheduling fix plus restart is recovery evidence, but
does not independently isolate scheduling from accumulated thread contention.
