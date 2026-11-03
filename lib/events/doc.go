// Package events provides a bounded event ring for agent-wow modules.
//
// It is a separate Go module from lib/wow: protocol encoding and event
// buffering have different consumers and can be versioned independently.
package events
