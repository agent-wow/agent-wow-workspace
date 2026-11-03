// Package wow holds protocol helpers shared by the agent-wow modules in this
// workspace: GUIDs, positions, movement packet encoding and object-update
// decoding for WoW 3.3.5a (build 12340).
//
// Modules must not duplicate this logic. Anything that translates between
// agent-wow's WorldPacket bytes and Go values belongs here.
package wow
