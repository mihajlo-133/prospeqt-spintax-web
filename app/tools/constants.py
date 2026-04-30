"""Shared constants and lightweight rule banks for spintax agent tooling."""

ROLE_VALUES = {"opener", "body", "proof", "cta", "ps", "unknown"}
FETCH_MODES = {"direct", "spider"}

SENSE_KEYWORDS = {
    "visual_observation": ["review", "saw", "noticed", "spotted", "site", "website"],
    "data_observation": ["sba", "data", "records", "files", "database", "report"],
    "discovery_inference": ["found", "found out", "based on what i found", "came across"],
    "send_share_cta": ["send", "show", "share", "walk through", "pass along"],
    "phone_number_cta": ["good number", "reach you", "number to reach"],
    "proof_growth": ["helped", "grew", "went from", "jump from", "add", "added"],
    "mechanism_help": ["help", "fund", "cover", "bridge", "grow"],
}

APPROVED_LEXICON = {
    "saw": {
        "approved": ["noticed", "found", "spotted", "came across"],
        "candidate_review": ["looked at", "picked up"],
        "rejected": ["observed", "ascertained", "identified", "examined"],
    },
    "send": {
        "approved": ["show", "share", "pass along"],
        "candidate_review": ["walk through", "go over"],
        "rejected": ["provide", "deliver", "transmit", "furnish"],
    },
    "show": {
        "approved": ["share", "send"],
        "candidate_review": ["walk through", "go over"],
        "rejected": ["demonstrate", "exhibit"],
    },
    "help": {
        "approved": ["support", "back"],
        "candidate_review": ["guide"],
        "rejected": ["assist", "facilitate", "enable", "empower", "optimize"],
    },
}

CTA_PERMISSION_MARKERS = ["want me to", "can i", "should i"]
CTA_CURIOSITY_MARKERS = ["would it hurt to", "worth seeing", "open to seeing"]
CTA_INTEREST_MARKERS = ["want to know more", "interested in seeing", "open to learning more"]
CTA_PHONE_MARKERS = ["good number to reach", "good number to call", "number to reach you"]

OBSERVATION_MARKERS = ["saw", "noticed", "spotted", "found", "came across", "records show"]
PROOF_MARKERS = ["we helped", "our product helped", "grew from", "went from", "jump from", "used our product"]
HYPOTHETICAL_MARKERS = ["what if", "suppose", "imagine"]
