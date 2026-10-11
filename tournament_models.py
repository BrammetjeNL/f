from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime

@dataclass
class Tournament:
    id: str
    name: str
    gamemode: str
    status: str  # DRAFT, REGISTRATION_OPEN, CHECK_IN, GENERATING_BRACKET, ACTIVE, DISPUTE, FINISHED, CANCELLED
    format: str  # SINGLE_ELIMINATION, DOUBLE_ELIMINATION
    max_players: int
    required_role: str
    minimum_rank: Optional[str] = None
    maximum_rank: Optional[str] = None
    forfeit_time: int = 300  # seconds
    prize: Optional[str] = None
    channel_id: Optional[int] = None
    ticket_category_id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    current_round: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Tournament':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

@dataclass
class Participant:
    tournament_id: str
    player_id: str  # Discord ID
    minecraft_username: str
    minecraft_uuid: str
    seed: int
    status: str  # REGISTERED, CHECKED_IN, DISQUALIFIED, FORFEITED
    joined_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Participant':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

@dataclass
class Match:
    id: str  # tournamentId:round:matchIndex
    tournament_id: str
    round: int
    player1_id: Optional[str]  # Discord ID
    player2_id: Optional[str]  # Discord ID
    player1_username: Optional[str] = None
    player2_username: Optional[str] = None
    player1_vote: Optional[str] = None  # winner player_id
    player2_vote: Optional[str] = None  # winner player_id
    winner_id: Optional[str] = None
    status: str = "WAITING_READY"  # WAITING_READY, ACTIVE, FINISHED, DISPUTED, FORFEITED
    ticket_channel_id: Optional[int] = None
    ready_timer_expires_at: Optional[str] = None
    evidence_urls: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Match':
        obj = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != 'evidence_urls'})
        obj.evidence_urls = data.get('evidence_urls', [])
        return obj
