package main

import (
	"context"
	"fmt"
	"regexp"
	"strings"

	"dagger/rask/internal/dagger"
)

// postgresValues is where the development fixture pins its Postgres image, the one pin the suite's server is read from
// too, so the server a test runs against and the server the estate runs cannot drift apart.
const postgresValues = "deploy/fixtures/postgres/chart/values.yaml"

// postgresPin reads the fixture's `image:` line: `postgres:<major>.<minor>@sha256:<digest>`.
var postgresPin = regexp.MustCompile(`(?m)^image:[ \t]*(postgres:([0-9]+)\.[0-9]+@sha256:[0-9a-f]{64})[ \t]*$`)

// postgresAssembly lays the image's server out as one directory that runs from wherever it is copied, as an ordinary
// user, with no package manager. Measured 2026-11-26 on aarch64 (glibc 2.39, ICU 74 on the host): the exported server
// ran initdb in 0.23 s and started in 0.10 s as uid 1001, from a directory other than the image's paths.
//
//   - Debian's bin and share directories keep their relative layout (`lib/postgresql/<major>/bin` beside
//     `share/postgresql/<major>`), which is how the server finds its share directory from its own path.
//   - The share directory is copied with its symlinks resolved: `postgresql.conf.sample` there is a link one level up,
//     and initdb fails without it.
//   - `libs/` holds every shared library the server's programs and modules load that glibc does not provide, read off
//     ldd in the image, so the install runs where those libraries are absent or another version (the host's ICU is 74,
//     the image's 76; the trixie-slim lane image has no libxml2, libldap or Kerberos). The libraries are the image's
//     own, for the image's architecture, so the same assembly serves aarch64 and x86_64 (x86_64 unmeasured). They need
//     glibc 2.39 or newer (libsystemd's highest symbol version, read with objdump on 2026-11-26 from the image's arm64
//     build): a host with an older glibc, Ubuntu 22.04's 2.35 among them, cannot run the export.
//   - The JIT provider and its bitcode are left out: they pull in LLVM and Z3 (144 MB of the 250 MB the libraries come
//     to with them), and the suite's server runs with `jit=off`.
const postgresAssembly = `set -eu
major="$1"
mkdir -p /out/lib/postgresql /out/share/postgresql /out/libs
cp -a "/usr/lib/postgresql/$major" /out/lib/postgresql/
rm -rf "/out/lib/postgresql/$major/lib/llvmjit.so" "/out/lib/postgresql/$major/lib/llvmjit_types.bc" "/out/lib/postgresql/$major/lib/bitcode"
cp -RL "/usr/share/postgresql/$major" /out/share/postgresql/
for object in "/out/lib/postgresql/$major/bin/"* "/out/lib/postgresql/$major/lib/"*.so; do ldd "$object" || true; done \
  | awk '$2 == "=>" && $3 ~ /^\// {print $3}' | sort -u | while read -r library; do
    case "$(dpkg -S "$(readlink -f "$library")" 2>/dev/null || echo unowned)" in
      libc6:*) ;;
      *) cp -L "$library" /out/libs/ ;;
    esac
  done
`

// postgresInstall is the fixture's pinned Postgres server as one relocatable directory: what `make postgres` exports to
// `.localbin/postgres` and what Test copies into its container at the same path, so root `conftest.py::postgres` finds
// the server in one place everywhere.
func postgresInstall(ctx context.Context, values *dagger.File) (*dagger.Directory, error) {
	contents, err := values.Contents(ctx)
	if err != nil {
		return nil, err
	}
	pin := postgresPin.FindStringSubmatch(contents)
	if pin == nil {
		return nil, fmt.Errorf("no `image: postgres:<major>.<minor>@sha256:<digest>` line in %s", postgresValues)
	}
	image, major := pin[1], pin[2]
	return dag.Container().
		From(image).
		WithExec([]string{"sh", "-c", strings.TrimSpace(postgresAssembly), "assemble", major}).
		Directory("/out"), nil
}

// PostgresInstall exports the development fixture's pinned Postgres server as one relocatable directory, the server
// the offline suite's Postgres fixture runs (`make postgres` writes it to `.localbin/postgres`).
func (m *Rask) PostgresInstall(
	ctx context.Context,
	// +defaultPath="/deploy/fixtures/postgres/chart/values.yaml"
	// +optional
	values *dagger.File,
) (*dagger.Directory, error) {
	return postgresInstall(ctx, values)
}
