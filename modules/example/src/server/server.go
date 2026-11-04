// Package server runs the module's gRPC server and session connection.
package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	modulev1 "github.com/agent-wow/go-module-template/src/api"
	"github.com/agent-wow/go-module-template/src/service"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
)

// Run serves the module until ctx is canceled, using sockets supplied by agent-wow.
func Run(ctx context.Context) error {
	moduleSocket := os.Getenv("AGENT_WOW_MODULE_SOCKET")
	sessionSocket := os.Getenv("AGENT_WOW_SESSION_SOCKET")
	if moduleSocket == "" || sessionSocket == "" {
		return fmt.Errorf("AGENT_WOW_MODULE_SOCKET and AGENT_WOW_SESSION_SOCKET must be set by agent-wow")
	}
	listener, err := net.Listen("unix", moduleSocket)
	if err != nil {
		return err
	}
	defer listener.Close()
	// agent-wow isolates this directory; the host must reach the container's socket.
	if err := os.Chmod(moduleSocket, 0666); err != nil {
		return err
	}
	conn, err := grpc.NewClient("unix://"+sessionSocket,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDisableRetry(),
		grpc.WithDefaultCallOptions(grpc.MaxRetryRPCBufferSize(0)),
	)
	if err != nil {
		return err
	}
	defer conn.Close()

	server := grpc.NewServer()
	defer server.Stop()
	modulev1.RegisterModuleServer(server, service.New(modv1.NewSessionClient(conn)))
	healthy := health.NewServer()
	grpc_health_v1.RegisterHealthServer(server, healthy)
	healthy.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	stop := context.AfterFunc(ctx, server.Stop)
	defer stop()
	if err := server.Serve(listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
		return err
	}
	return nil
}
