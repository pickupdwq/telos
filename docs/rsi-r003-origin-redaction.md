# R003: executor failure origin redaction

`peer_turn` builds the failure origin from the complete executor exception text.
It redacts every nonempty upstream executor credential and relay token in that
full text before truncating the sanitized payload to 1,200 characters. The
existing trailing `; ` separator and execution-deadline prefix are then applied.

Redacting before truncation prevents a credential that crosses the length
boundary from leaving a recognizable prefix in the origin. Truncating before
replacement could also expand the payload when a short credential is replaced,
so the 1,200-character limit applies to the sanitized text.

This boundary ordering does not change budget checks, retry policy, exception
types, snapshots, thread identifiers, or timeout metadata.
