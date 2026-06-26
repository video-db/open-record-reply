"""Shared server state singleton."""

from dataclasses import dataclass, field


@dataclass
class ServerState:
    conn: object = None
    coll: object = None
    ws_connection_id: str = ""

    is_recording: bool = False
    recording_skill_name: str = ""
    recording_start_epoch_ms: int = 0
    effective_recording_start_epoch_ms: int = 0
    effective_recording_end_epoch_ms: int = 0
    ax_proc: object = None
    ax_subproc: object = None
    ax_client: object = None
    capture_client: object = None
    capture_session: object = None
    events_path: str = ""
    session_dir: str = ""

    ws_events: list = field(default_factory=list)


state = ServerState()
