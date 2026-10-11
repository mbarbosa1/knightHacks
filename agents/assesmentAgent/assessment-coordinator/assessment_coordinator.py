'''
Assessment coordinator ("merges results" in the architecture diagram): plain code, no AI.

After detect and medical have run on a batch, it joins what they said about each incident into one record
and hands that to the response stage (resource ‖ route -> plan):

    merge({'detect_agent': result, 'medical_agent': result}, batch)
    -> {"cursor", "situation_summary", "incidents": [...most urgent first], "road_restrictions": [...],
        "totals": {...}, "needs_response": [incident ids with priority critical/high], "agents": {name: "ok" | error}}

Per incident: location, status, building, prediction and evidence come from detect; person_groups, triage,
ambulances and extrication come from medical; priority is the more urgent of the two. Both agents already
wrote their changes to Neon, so this is a hand-off summary, not a second write.
'''
PRIORITY = ('critical', 'high', 'medium', 'low')
DETECT_FIELDS = ('status', 'location', 'uncertainty_m', 'building_id', 'prediction', 'evidence')
MEDICAL_FIELDS = ('person_groups', 'needs_ambulances', 'needs_extrication', 'notes')


def _rank(priority):
    return PRIORITY.index(priority) if priority in PRIORITY else len(PRIORITY)


def _incidents(result):
    out = (result or {}).get('output')
    return [i for i in (out or {}).get('incidents') or [] if isinstance(i, dict)] if isinstance(out, dict) else []


def merge(results, batch):
    detect, medical = results.get('detect_agent'), results.get('medical_agent')
    merged, unidentified = {}, 0
    for source, fields, prio_key in ((detect, DETECT_FIELDS, 'priority'), (medical, MEDICAL_FIELDS, 'medical_priority')):
        for inc in _incidents(source):
            iid = inc.get('incident_id')
            if not iid:
                unidentified += 1
                continue
            row = merged.setdefault(iid, {'incident_id': iid, 'priority': None, 'changed_by': []})
            row.update({f: inc[f] for f in fields if inc.get(f) is not None})
            if inc.get('change') in ('created', 'updated'):
                row['changed_by'].append('detect' if source is detect else 'medical')
            if _rank(inc.get(prio_key)) < _rank(row['priority']):
                row['priority'] = inc.get(prio_key)
    incidents = sorted(merged.values(), key=lambda r: (_rank(r['priority']), r['incident_id']))
    summaries = [((r or {}).get('output') or {}).get('situation_summary') for r in (detect, medical)]
    return {
        'cursor': batch[-1]['observation_id'] if batch else None,
        'observation_ids': [r['observation_id'] for r in batch],
        'situation_summary': ' '.join(s for s in summaries if s) or None,
        'incidents': incidents,
        'road_restrictions': ((detect or {}).get('output') or {}).get('road_restrictions') or [],
        'totals': ((medical or {}).get('output') or {}).get('totals') or {},
        'needs_response': [r['incident_id'] for r in incidents if r['priority'] in ('critical', 'high')],
        'agents': {name: ('ok' if 'error' not in r else r['error']) for name, r in results.items()},
        'unidentified_incidents': unidentified,
    }
