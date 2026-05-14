"""AgentCard definitions for the A2A protocol.

We serve two cards from the same app:

- Public card (`/.well-known/agent-card.json`) advertises the open `/a2a`
  route with advisory tools only (EXA + web_fetch).
- Privileged card (`/.well-known/agent-card/privileged.json`) advertises the
  bearer-gated `/a2a/privileged` route which additionally exposes Composio
  side-effect tools (Gmail drafts, Attio writes) into Sedale's environment.

The shapes follow the a2a-sdk types as used by the helloworld example in
github.com/a2aproject/a2a-samples/samples/python/agents/helloworld.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)


CAPITAL_FORMATION_SKILL = AgentSkill(
    id="capital_formation_advisory",
    name="Capital formation advisory for infrastructure-scale projects",
    description=(
        "Evaluate project bankability and design a dynamic capital stack — "
        "non-dilutive (federal grants, IRA tax credits, state programs, "
        "DOE LPO, philanthropic, PRI/MRI), project finance (senior debt, "
        "mezz, tax equity), and equity — with a 12-month sequencing plan "
        "and named opportunities cited to source."
    ),
    tags=[
        "capital-formation",
        "project-finance",
        "infrastructure",
        "energy",
        "transmission",
        "broadband",
        "water",
        "ports",
        "semiconductors",
        "manufacturing",
        "doe-lpo",
        "ira-tax-credits",
    ],
    examples=[
        "Build a capital stack for a 200MW solar + 100MWh storage project in New Mexico. IOU offtake, CapEx ~$280M.",
        "What non-dilutive capital fits a Class III broadband middle-mile build in rural Montana?",
        "Sequence funding for a 500MW transmission upgrade through ERCOT.",
        "Evaluate DOE LPO Title 17 fit for a 100MW green hydrogen project with an industrial offtaker.",
        "Design the capital stack for a $1.2B advanced packaging fab in Arizona.",
        "What's the right transferability-vs-direct-pay election for a tax-exempt cooperative on a 45Y PTC?",
    ],
)


@dataclass(frozen=True)
class CardSet:
    public: AgentCard
    privileged: AgentCard


_PUBLIC_DESC = (
    "AI agent trained on Sedale Turbovsky's approach to capital "
    "formation for infrastructure-scale projects ($50M–$5B+). "
    "Not the real Sedale. Advisory only — no commitments, no "
    "investment advice. Capital stacks involving securities "
    "require qualified securities counsel."
)

_PRIVILEGED_DESC = (
    _PUBLIC_DESC
    + " Privileged endpoint additionally exposes Composio side-effects "
    "(Gmail drafts, Attio notes/deals) into Sedale's environment. "
    "Requires Authorization: Bearer <SEDALE_GPT_PRIVILEGED_TOKEN>."
)


def _build_card(*, name: str, description: str, public_url: str, route_path: str) -> AgentCard:
    # AgentCard is a protobuf-backed type; repeated fields cannot be reassigned
    # after construction, so we pass everything in here.
    return AgentCard(
        name=name,
        description=description,
        version="1.0.0",
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/markdown", "application/json"],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=False,
        ),
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=f"{public_url}{route_path}"),
        ],
        skills=[CAPITAL_FORMATION_SKILL],
    )


def build_card_set(public_url: str | None = None) -> CardSet:
    public_url = (public_url or os.environ.get("PUBLIC_URL") or "http://127.0.0.1:8080").rstrip("/")

    public = _build_card(
        name="Sedale GPT",
        description=_PUBLIC_DESC,
        public_url=public_url,
        route_path="/a2a",
    )
    privileged = _build_card(
        name="Sedale GPT (privileged)",
        description=_PRIVILEGED_DESC,
        public_url=public_url,
        route_path="/a2a/privileged",
    )
    return CardSet(public=public, privileged=privileged)
