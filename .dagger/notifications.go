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
// or `make notifications-rig`, which brings this and the Slack sink up together.
func (m *Rask) NotificationsMailpit() *dagger.Service {
	return dag.Container().
		From(mailpitImage).
		WithEnvVariable("MP_SMTP_AUTH_ACCEPT_ANY", "true").
		WithEnvVariable("MP_SMTP_AUTH_ALLOW_INSECURE", "true").
		WithExposedPort(1025).
		WithExposedPort(8025).
		AsService()
}

// NotificationsSlackSink is the rig's webhook half — a stand-in that COUNTS rather than merely
// accepting, so it can say "this arrived twice" without a human reading a log.
//
// The server is `scripts/slack_sink.py`, a real file. It used to be a 27-line HTTP server embedded as
// an inline `command:` heredoc inside the compose YAML, where nothing could lint it, type-check it or
// run it on its own; extracting it was the larger half of retiring that stack.
//
//	dagger call notifications-slack-sink up --ports=9099:9099
func (m *Rask) NotificationsSlackSink(
	// +defaultPath="/scripts/slack_sink.py"
	sink *dagger.File,
) *dagger.Service {
	return dag.Container().
		From(UvPythonImage).
		WithFile("/srv/slack_sink.py", sink).
		WithExposedPort(9099).
		WithDefaultArgs([]string{"python", "/srv/slack_sink.py"}).
		AsService()
}
