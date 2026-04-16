"""In-process shared state (leads, outreach, perf records).

All tool modules import from here so a single store is shared across the
lifetime of the Flask process.
"""

_leads_store: list = []
_outreach_store: list = []
_perf_store: list = []


def get_leads() -> list:
    return _leads_store


def set_leads(leads: list) -> None:
    global _leads_store
    _leads_store = leads


def get_outreach() -> list:
    return _outreach_store


def set_outreach(drafts: list) -> None:
    global _outreach_store
    _outreach_store = drafts


def get_perf() -> list:
    return _perf_store


def append_perf(record: dict) -> None:
    global _perf_store
    _perf_store.append(record)
    if len(_perf_store) > 200:
        _perf_store = _perf_store[-200:]
