package main

import (
	"dagger/rask/internal/dagger"
)

// mailpitImage is the SMTP sink the channel drive sends through. Pinned to the version the retired
// compose stack used.
const mailpitImage = "axllent/mailpit:v1.21"

// NotificationsMailpit is the rig's SMTP half: point the Dapr `bindings.smtp` component's `host` at
// :1025 and read every message the plane sent at :8025.
//
// Mailpit over a real relay on purpose — it accepts everything, stores it, and shows it, so a
// DUPLICATE is visible rather than inferred from a provider's rate limit. That is the whole question
// the rig exists for: does exactly ONE message leave the estate per (event, subject, channel) when
// the broker redelivers?
//
//	dagger call notifications-mailpit up --ports=1025:1025 --ports=8025:8025
//
// or `make notifications-rig`.
func (m *Rask) NotificationsMailpit() *dagger.Service {
	return dag.Container().
		From(mailpitImage).
		WithEnvVariable("MP_SMTP_AUTH_ACCEPT_ANY", "true").
		WithEnvVariable("MP_SMTP_AUTH_ALLOW_INSECURE", "true").
		WithExposedPort(1025).
		WithExposedPort(8025).
		AsService()
}
