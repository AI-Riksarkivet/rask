# 0061. Do not add a control before its verifier (2026-09-07)

§F2-12 asks for signed and attested images, and the obvious move is to add cosign to the publish
path. Measuring first says not to, for a reason this backlog has produced six times in one day.

**The estate has ZERO signature verifiers.** No Kyverno, no sigstore policy-controller, no
Gatekeeper; its five validating webhooks are CNPG, external-secrets and Kueue. So a signature added
today would be created, pushed, and checked by nothing — a control whose NAME is present and whose
enforcement is absent. That is the same shape as `provenance()` emitting three OCI labels, as a
credential the transport overwrites, and as a field passed and read by nothing. Adding it would make
this section's seventh member of its own anti-pattern, and the person who later asks "are our images
signed?" would find a yes.

**Signing is not a code change; it is a custody question and a verification question.** Where does
the key live, who may use it, and what refuses an unsigned image at admission? Until the last of
those exists, the signature is a claim nobody checks.

**The SBOM half is genuinely separable, and worth judging on its own terms.** The estate already
answers "what vulnerable things are in this?" three ways — `make audit` (osv-scanner over six
lockfiles plus `.dagger/go.mod`), `make scan-config` (trivy over `.docker/` and `chart/`), and
`make scan-image` (trivy over a Dagger-built image). What an SBOM adds is a PORTABLE manifest a
downstream consumer can scan without rebuilding: a supply-chain claim, not a scanning gap. That may
be worth doing; it is not worth doing as half of a signature story with no verifier.

**The general rule: a control needs its enforcement point identified before it is built.** Ask what
would REFUSE the thing this control exists to catch, and if the answer is nothing, the work is to
build the refusal — not the artifact it would inspect.
