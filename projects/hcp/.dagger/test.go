package main

import (
	"context"
	"fmt"
	"time"

	"dagger/ra-hcp/internal/dagger"
)

// Test runs pytest on the backend.
func (m *RaHcp) Test(ctx context.Context, source *dagger.Directory) (string, error) {
	return m.buildBackendDev(source).
		WithExec([]string{"uv", "run", "pytest"}).
		Stdout(ctx)
}

// TestCoverage runs pytest with coverage reporting.
func (m *RaHcp) TestCoverage(ctx context.Context, source *dagger.Directory) (string, error) {
	return m.buildBackendDev(source).
		WithExec([]string{"uv", "run", "pytest", "--cov"}).
		Stdout(ctx)
}

// TestIntegration runs integration tests with a real Redis service.
func (m *RaHcp) TestIntegration(ctx context.Context, source *dagger.Directory) (string, error) {
	redisSvc := m.redis()

	return m.buildBackendDev(source).
		WithServiceBinding("redis", redisSvc).
		WithEnvVariable("REDIS_URL", "redis://redis:6379").
		WithExec([]string{"uv", "run", "pytest", "-m", "redis"}).
		Stdout(ctx)
}

// TestMinioIntegration runs S3 integration tests against a real MinIO service.
func (m *RaHcp) TestMinioIntegration(ctx context.Context, source *dagger.Directory) (string, error) {
	minioSvc := m.minio()

	// Cache-bust: ensure pytest always runs fresh (service bindings need a live service)
	cacheBuster := fmt.Sprintf("minio-test-%d", time.Now().UnixNano())

	return m.buildBackendDev(source).
		WithServiceBinding("minio", minioSvc).
		WithEnvVariable("MINIO_ENDPOINT", "http://minio:9000").
		WithEnvVariable("S3_ACCESS_KEY", minioRootUser).
		WithEnvVariable("S3_SECRET_KEY", minioRootPassword).
		WithEnvVariable("CACHE_BUSTER", cacheBuster).
		WithExec([]string{"uv", "run", "pytest", "-m", "minio", "-v", "--tb=short"}).
		Stdout(ctx)
}
