"""Check real native observations exported by aa-accountability.seam.e2e.test.ts.

Set AA_TEST_OBSERVATIONS to a fresh JSONL path for the native test, then pass that
path to this script with the revision's four packages on PYTHONPATH.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aa_openclaw


def check(path):
    batches = [json.loads(line) for line in Path(path).read_text().splitlines() if line]
    expected = {f'refused-{kind}' for kind in ('security', 'input', 'elevation', 'host', 'node')}
    assert len(batches) == len(expected), 'export must contain one fresh run of the five refusals'
    assert {batch['native_session_id'] for batch in batches} == expected
    for batch in batches:
        sid = batch['native_session_id']
        aa_openclaw.begin_session(party='0xalice', native_session_id=sid, consent_tools=['exec'])
        for event in batch['events']:
            aa_openclaw.observe(dict(native_session_id=sid, event_id=event['eventId'],
                action_id=event['actionId'], phase=event['phase'], tool=event['toolName'],
                args=event['params'], result=event.get('result')))
        acc, session = aa_openclaw.current(sid)
        assert len(session.records) == 1
        assert all(not verdict.violated for verdict in acc.self_check(session.records).values())
        print(f'{sid}: one typed nonexecution record; no predicate violation')


if __name__ == '__main__':
    check(sys.argv[1])
