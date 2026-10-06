# flybridge-orca

Orca adapter for Flybridge workflows.

The current public Orca API cannot atomically guard an existing PTY prompt against agent exit or receiver takeover. `send_prompt` always raises `PromptDeliveryBlocked` without writing input. `agent_owner_state` is `unknown`: terminal presence, agent labels and idle signals are not actual agent-process liveness. Prompt-argument launches remain supported; startup PTY injection and reused-terminal resume are unavailable. See the receiver-safe notification boundary in [the specification](../../docs/en/specification.md).
