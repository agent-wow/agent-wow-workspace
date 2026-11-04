package service

import (
	"context"
	"encoding/binary"
	"sync"
	"testing"
	"time"

	modv1 "github.com/agent-wow/agent-wow/pkg/modules/v1"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type fakeSession struct {
	modv1.SessionClient
	clock func(context.Context) (*modv1.ClockResponse, error)
	send  func(context.Context, *modv1.SendPacketRequest) (*emptypb.Empty, error)
}

func (f fakeSession) GetClock(ctx context.Context, _ *emptypb.Empty, _ ...grpc.CallOption) (*modv1.ClockResponse, error) {
	return f.clock(ctx)
}

func (f fakeSession) SendPacket(ctx context.Context, req *modv1.SendPacketRequest, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	return f.send(ctx, req)
}

func testService() *Service {
	return New(fakeSession{clock: func(context.Context) (*modv1.ClockResponse, error) {
		return &modv1.ClockResponse{ClientTimeMs: 123}, nil
	}})
}

func timePacket(timestamp uint32) *modv1.WorldPacket {
	body := binary.LittleEndian.AppendUint32(nil, timestamp)
	body = binary.LittleEndian.AppendUint32(body, 3600)
	return &modv1.WorldPacket{Opcode: opcode.SMSGQueryTimeResponse, Payload: body}
}

func TestPacketStatus(t *testing.T) {
	s := testService()
	ctx := t.Context()
	before, err := s.Status(ctx, &emptypb.Empty{})
	if err != nil || before.GetClientTimeMs() != 123 || before.ServerTimeUnix != nil || before.PacketsReceived != 0 {
		t.Fatalf("initial status = %v, %v", before, err)
	}
	for _, timestamp := range []uint32{0, 1790812800} {
		if _, err := s.OnPacket(ctx, timePacket(timestamp)); err != nil {
			t.Fatal(err)
		}
		response, err := s.Status(ctx, &emptypb.Empty{})
		if err != nil || response.ServerTimeUnix == nil || response.GetServerTimeUnix() != timestamp {
			t.Fatalf("status after packet = %v, %v", response, err)
		}
	}
	for _, packet := range []*modv1.WorldPacket{
		nil,
		{Opcode: opcode.SMSGPong, Payload: make([]byte, 8)},
		{Opcode: opcode.SMSGQueryTimeResponse, Payload: make([]byte, 7)},
		{Opcode: opcode.SMSGQueryTimeResponse, Payload: make([]byte, 9)},
	} {
		if _, err := s.OnPacket(ctx, packet); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("malformed packet error = %v", err)
		}
	}
	after, err := s.Status(ctx, &emptypb.Empty{})
	if err != nil || after.GetPacketsReceived() != 2 || after.GetServerTimeUnix() != 1790812800 {
		t.Fatalf("status after malformed packets = %v, %v", after, err)
	}
}

func TestCallbacks(t *testing.T) {
	for _, mode := range []string{"success", "unavailable", "canceled", "deadline"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			var wantErr error
			switch mode {
			case "unavailable":
				wantErr = status.Error(codes.Unavailable, "session unavailable")
			case "canceled":
				cancel()
				wantErr = ctx.Err()
			case "deadline":
				var end context.CancelFunc
				ctx, end = context.WithDeadline(ctx, time.Now().Add(-time.Second))
				defer end()
				wantErr = ctx.Err()
			}
			clockCalls, sendCalls := 0, 0
			s := New(fakeSession{
				clock: func(got context.Context) (*modv1.ClockResponse, error) {
					clockCalls++
					if got != ctx {
						t.Error("GetClock did not preserve context")
					}
					return &modv1.ClockResponse{ClientTimeMs: 123}, wantErr
				},
				send: func(got context.Context, req *modv1.SendPacketRequest) (*emptypb.Empty, error) {
					sendCalls++
					if got != ctx || req.Opcode != opcode.CMSGQueryTime || len(req.Payload) != 0 {
						t.Errorf("unexpected SendPacket context or packet: %v", req)
					}
					return &emptypb.Empty{}, wantErr
				},
			})
			if _, err := s.Status(ctx, &emptypb.Empty{}); err != wantErr {
				t.Fatalf("Status error = %v, want %v", err, wantErr)
			}
			if _, err := s.QueryTime(ctx, &emptypb.Empty{}); err != wantErr {
				t.Fatalf("QueryTime error = %v, want %v", err, wantErr)
			}
			if clockCalls != 1 || sendCalls != 1 {
				t.Fatalf("callback counts: clock=%d send=%d", clockCalls, sendCalls)
			}
		})
	}
}

func TestConcurrentPacketsAndStatus(t *testing.T) {
	s := testService()
	var wg sync.WaitGroup
	for range 50 {
		wg.Go(func() {
			if _, err := s.OnPacket(t.Context(), timePacket(42)); err != nil {
				t.Error(err)
			}
		})
		wg.Go(func() {
			response, err := s.Status(t.Context(), &emptypb.Empty{})
			if err != nil {
				t.Error(err)
				return
			}
			if response.PacketsReceived > 0 && (response.ServerTimeUnix == nil || response.GetServerTimeUnix() != 42) {
				t.Errorf("inconsistent snapshot: %v", response)
			}
		})
	}
	wg.Wait()
	response, err := s.Status(t.Context(), &emptypb.Empty{})
	if err != nil || response.GetPacketsReceived() != 50 {
		t.Fatalf("final status = %v, %v", response, err)
	}
}
