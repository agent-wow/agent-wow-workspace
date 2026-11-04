package server

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/agent-wow/go-module-template/src/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type sessionServer struct {
	modv1.UnimplementedSessionServer
	sends atomic.Uint32
}

func (*sessionServer) GetClock(context.Context, *emptypb.Empty) (*modv1.ClockResponse, error) {
	return &modv1.ClockResponse{ClientTimeMs: 456}, nil
}

func (s *sessionServer) SendPacket(_ context.Context, req *modv1.SendPacketRequest) (*emptypb.Empty, error) {
	s.sends.Add(1)
	if req.Opcode != opcode.CMSGQueryTime || len(req.Payload) != 0 {
		return nil, status.Error(codes.InvalidArgument, "unexpected packet")
	}
	return nil, status.Error(codes.Unavailable, "simulated write failure")
}

func TestUnixSocketLifecycle(t *testing.T) {
	dir := t.TempDir()
	moduleSocket := filepath.Join(dir, "module.sock")
	sessionSocket := filepath.Join(dir, "session.sock")
	t.Setenv("AGENT_WOW_MODULE_SOCKET", moduleSocket)
	t.Setenv("AGENT_WOW_SESSION_SOCKET", sessionSocket)

	listener, err := net.Listen("unix", sessionSocket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	callback := &sessionServer{}
	server := grpc.NewServer()
	modv1.RegisterSessionServer(server, callback)
	go server.Serve(listener)
	defer server.Stop()

	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(5 * time.Second):
			t.Error("module did not stop after cancellation")
		}
		if _, err := os.Stat(moduleSocket); !errors.Is(err, os.ErrNotExist) {
			t.Errorf("module socket was not removed: %v", err)
		}
	})

	conn, err := grpc.NewClient("unix://"+moduleSocket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	callCtx, end := context.WithTimeout(t.Context(), 5*time.Second)
	defer end()
	health := grpc_health_v1.NewHealthClient(conn)
	check, err := health.Check(callCtx, &grpc_health_v1.HealthCheckRequest{}, grpc.WaitForReady(true))
	if err != nil || check.GetStatus() != grpc_health_v1.HealthCheckResponse_SERVING {
		t.Fatalf("health = %v, %v", check, err)
	}
	watch, err := health.Watch(callCtx, &grpc_health_v1.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	update, err := watch.Recv()
	if err != nil || update.GetStatus() != grpc_health_v1.HealthCheckResponse_SERVING {
		t.Fatalf("health watch = %v, %v", update, err)
	}
	info, err := os.Stat(moduleSocket)
	if err != nil || info.Mode().Perm() != 0666 {
		t.Fatalf("socket permissions = %v, %v", info, err)
	}
	client := modulev1.NewModuleClient(conn)
	if _, err := client.QueryTime(callCtx, &emptypb.Empty{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("QueryTime error = %v", err)
	}
	if callback.sends.Load() != 1 {
		t.Fatalf("SendPacket was retried: %d calls", callback.sends.Load())
	}
	payload := binary.LittleEndian.AppendUint32(nil, 1234)
	payload = binary.LittleEndian.AppendUint32(payload, 3600)
	if _, err := client.OnPacket(callCtx, &modv1.WorldPacket{Opcode: opcode.SMSGQueryTimeResponse, Payload: payload}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.BeforeLogout(callCtx, &emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	response, err := client.Status(callCtx, &emptypb.Empty{})
	if err != nil || response.GetClientTimeMs() != 456 || response.GetServerTimeUnix() != 1234 || response.GetPacketsReceived() != 1 {
		t.Fatalf("status after logout hook = %v, %v", response, err)
	}
	check, err = health.Check(callCtx, &grpc_health_v1.HealthCheckRequest{})
	if err != nil || check.GetStatus() != grpc_health_v1.HealthCheckResponse_SERVING {
		t.Fatalf("health after logout hook = %v, %v", check, err)
	}
}

func TestMissingSocketEnvironment(t *testing.T) {
	for _, missing := range []string{"AGENT_WOW_MODULE_SOCKET", "AGENT_WOW_SESSION_SOCKET"} {
		t.Run(missing, func(t *testing.T) {
			t.Setenv("AGENT_WOW_MODULE_SOCKET", filepath.Join(t.TempDir(), "module.sock"))
			t.Setenv("AGENT_WOW_SESSION_SOCKET", filepath.Join(t.TempDir(), "session.sock"))
			t.Setenv(missing, "")
			if err := Run(t.Context()); err == nil {
				t.Fatal("missing environment should fail startup")
			}
		})
	}
}
