# Fixtures: NOT FOR PRODUCTION

Throwaway installs of what the rask chart takes as a prerequisite, so a development cluster or a CI lane has
them (ADR 0153). A production cluster gets each from its platform team, at the version and with the settings that
team supports; nothing here is a supported install of anything.

Each directory is one fixture, installed as **its own Helm release** before the rask chart:

| File | What it holds |
| --- | --- |
| `fixture.env` | `RELEASE`, `NAMESPACE`, `REPO`, `CHART`, `VERSION` of the upstream chart; `ADOPT_CRDS`, when set, names CRDs another release owns today |
| `values.yaml` | optional: the upstream chart's values |

`scripts/fixtures.sh <name>...` installs them. `make k3s-up` runs it with `FIXTURES` (default `external-secrets`)
before the rask release. A GPU node on which nothing advertises `nvidia.com/gpu` yet adds `nvidia-device-plugin`:

```bash
kubectl get nodes -o jsonpath='{.items[*].status.allocatable}' | tr ' ' '\n' | grep nvidia   # empty: no plugin runs
kubectl get ds -A | grep -i nvidia                                                           # nor a DaemonSet
make k3s-up FIXTURES="external-secrets nvidia-device-plugin"
```

Never beside a plugin already running (the NVIDIA GPU Operator, a k3s add-on): two plugins on one node both register
`nvidia.com/gpu` with the kubelet, which keeps whichever registered last. The fixture installs no `nvidia`
RuntimeClass: k3s creates and manages that object itself once the nvidia container toolkit is on the host.

Every fixture's namespace and release carry the mark: the namespace is labelled
`rask.dev/fixture=not-for-production`, and the release description says the same (`helm history <release> -n <ns>`).

## Handing CRDs to a fixture

A subchart that leaves the rask chart takes its CRDs with it, and Helm deletes a CRD, and every object of its kind,
when the release that owns it stops rendering it. `ADOPT_CRDS` hands them over instead: before installing, the
script annotates each named CRD `helm.sh/resource-policy=keep`, so no upgrade or uninstall of the old release deletes
it, and installs the fixture with `--take-ownership`, so the fixture's release becomes its owner. Run the fixture
before the rask upgrade that drops the subchart.
