# Always hash entity keys that are not surrogate types

Entity-key values are the only row-level data allowed to reach the LLM (OpenAI API). Natural keys are often personal data (email, phone, username), and once a value has been sent to a third party it can't be taken back. So any entity key that isn't a surrogate type (integer types, `uuid`) is always replaced with a stable hash before it reaches the LLM, whatever `privacy.hash_entity_ids` says. That setting only controls surrogate-type keys. The UI un-hashes values locally for display.

## Considered Options

- **Config-only hashing (`hash_entity_ids`, off by default):** rejected. Confirming an LLM-proposed `email` entity key would send raw emails to OpenAI with nothing to stop it.
- **Reject non-surrogate entity keys outright:** rejected. Some real schemas key tenants by slug or code, and hotspot analysis should still work for them.

## Consequences

- When the LLM reasons about a text-keyed hotspot it only sees hashes, so explanations can't name the entity. The UI fills in the real value.
- Hashing is HMAC-SHA256 with a per-Connection secret kept outside SQLite. A plain hash could be reversed by enumerating short keys. Losing the secret changes every hashed ID.
- The entity-map confirmation step shows each key's type, so the user can see which keys will be hashed.
