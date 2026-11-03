package server

import (
	"os"
	"path/filepath"
	"testing"

	moddisc "github.com/agent-wow/agent-wow/pkg/modules/discovery"
	"github.com/agent-wow/agent-wow/pkg/opcode"
	modulev1 "github.com/agent-wow/go-module-template/src/api"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/types/descriptorpb"
)

func TestModulePackage(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "example")
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"module.yaml", "compose.yaml", "module.pb"} {
		body, err := os.ReadFile(filepath.Join("..", "..", name))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), body, 0644); err != nil {
			t.Fatal(err)
		}
	}
	registry, err := moddisc.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	module, ok := registry.Lookup("example")
	if !ok || len(module.Manifest().RPC) != 2 || len(module.Manifest().Requires) != 0 {
		t.Fatal("expected one standalone module with two public methods")
	}
	for alias, path := range map[string]string{
		"status": modulev1.Module_Status_FullMethodName, "query_time": modulev1.Module_QueryTime_FullMethodName,
	} {
		method, ok := module.RPC(alias)
		if !ok || method.Path() != path {
			t.Fatalf("wrong mapping for %s", alias)
		}
	}
	packet, ok := module.Packet(opcode.SMSGQueryTimeResponse)
	if !ok || packet.Path() != modulev1.Module_OnPacket_FullMethodName {
		t.Fatal("packet handler is not mapped")
	}
	hook, ok := module.BeforeLogout()
	if !ok || hook.Path() != modulev1.Module_BeforeLogout_FullMethodName {
		t.Fatal("logout hook is not mapped")
	}
	body, err := os.ReadFile(filepath.Join("..", "..", "module.pb"))
	if err != nil {
		t.Fatal(err)
	}
	var set descriptorpb.FileDescriptorSet
	if err := proto.Unmarshal(body, &set); err != nil {
		t.Fatal(err)
	}
	for _, file := range set.File {
		if file.GetName() == "module.proto" {
			if !proto.Equal(file, protodesc.ToFileDescriptorProto(modulev1.File_module_proto)) {
				t.Fatal("descriptor and Go bindings differ; run make generate")
			}
			return
		}
	}
	t.Fatal("module.proto is missing from the descriptor set")
}
