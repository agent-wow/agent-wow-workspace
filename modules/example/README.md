# Go module template for agent-wow

A reference module template for agent-wow.

This example queries the worldserver's time and exposes the latest reply through
an RPC, demonstrating packet sending and receiving, the session clock, and a logout hook.

**Refer to the [agent-wow repository](https://github.com/agent-wow/agent-wow) for the full documentation on modules.**

## Structure

```text
.
├── cmd/main.go    # Entrypoint
├── src/
│   ├── api/       # Generated protobuf and gRPC bindings
│   ├── server/    # Connections, health checks, and shutdown
│   └── service/   # RPC, packet, and logout handlers
├── module.yaml    # Module settings and handler mappings
├── module.proto   # Messages and service definitions
├── module.pb      # Generated descriptor for agent-wow
├── compose.yaml   # Container configuration
└── Dockerfile     # Container build
```

## Usage

Clone the template into your modules directory, then start a character session:

```sh
mkdir -p ~/.config/agent-wow/modules
git clone https://github.com/agent-wow/go-module-template.git \
  ~/.config/agent-wow/modules/example
agent-wow module list
agent-wow char play <character-name>
```

Use your configured `module_dir` if it differs from the default. agent-wow builds
and starts the container automatically; no local Go or protobuf tools are needed.
The installed directory name determines the RPC prefix (`example` here).

Once the character is in the world, call the module from another terminal:

```sh
# Request the worldserver's time.
curl -sS http://localhost:8086/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"example.query_time","params":{}}'

# Read the latest reply, packet count, and session clock.
curl -sS http://localhost:8086/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"example.status","params":{}}'

# Run the logout hook and end the session.
curl -sS http://localhost:8086/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"session.logout"}'
```

To customize the template, edit the handlers in `src/service` and their mappings
in `module.yaml`. After changing `module.proto`, run `make generate` with Go
1.27.1 or newer and `protoc` installed to update the bindings and descriptor.
