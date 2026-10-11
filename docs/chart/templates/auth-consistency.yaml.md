# chart/templates/auth-consistency.yaml

Rationale for `chart/templates/auth-consistency.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.auth.enabled .Values.frontend.enabled (not .Values.frontend.oidc.enabled) (not .Values.a...`

AUTH CONSISTENCY — refuse a HALF-GOVERNED deploy.

Renders no object. It exists to fail the render on one combination that otherwise installs
cleanly and then greets every user with a wall they cannot get past:

    auth.enabled: true          # the backend demands a verified identity
    frontend.oidc.enabled: false # the UI has no sign-in flow at all

That stack comes up green. Every pod is Ready, helm reports deployed, and the frontend renders
"This stack is governed — sign in to browse projects" above a page with NO WAY TO SIGN IN, because
the home zone's /auth/{login,callback,logout} BFF only exists when frontend.oidc.enabled is set.
The catalog then 401s every read the browser attempts. Nothing in the install says which of the two
halves was forgotten, and the symptom (an empty, scolding UI) looks like a data problem rather than
a configuration one.

The chart already guards the OPPOSITE direction — frontend.oidc.enabled without a
publicIssuer or publicOrigin fails (frontends.yaml, _helpers.tpl). This is the missing half.

Two combinations are deliberately NOT errors:
  * frontend.enabled=false — there is no UI to sign into, so a governed API-only stack is coherent.
    This is what the local dev render produces.
  * auth.allowHeadless=true — an explicit, named opt-in for an API-only deploy that DOES ship the
    frontend (a machine-facing estate where humans never use the browser). Someone choosing that
    must say so; the point of this guard is that it cannot happen by omission.
