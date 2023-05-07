import socket
from typing import Optional, List
from pathlib import Path

from pydantic import BaseModel, Field

from .packets import IdentityPacket


class HostConfig(BaseModel):
    device_name: str = Field(default_factory=socket.gethostname)
    device_id: str = ''
    device_type: str = 'laptop'

    incoming_capabilities: List[str] = []
    outgoing_capabilities: List[str] = []

    device_certfile: Optional[Path] = None
    device_keyfile: Optional[Path] = None

    could_send_my_id_packs: bool = False

    def generate_IdentityPacket(self):
        return IdentityPacket.generate(**self.dict())
