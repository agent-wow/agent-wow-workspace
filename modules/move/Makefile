.PHONY: generate
generate:
	go mod download github.com/agent-wow/agent-wow google.golang.org/protobuf google.golang.org/grpc/cmd/protoc-gen-go-grpc
	go build -o bin/ google.golang.org/protobuf/cmd/protoc-gen-go google.golang.org/grpc/cmd/protoc-gen-go-grpc
	protoc -I . -I "$$(go list -m -f '{{.Dir}}' github.com/agent-wow/agent-wow)" \
		--plugin=protoc-gen-go=bin/protoc-gen-go \
		--plugin=protoc-gen-go-grpc=bin/protoc-gen-go-grpc \
		--go_out=. --go_opt=module=$$(go list -m) \
		--go-grpc_out=. --go-grpc_opt=module=$$(go list -m) \
		--include_imports --descriptor_set_out=module.pb module.proto
