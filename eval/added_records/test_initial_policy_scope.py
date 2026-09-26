from . import initial_policy_scope as experiment


def test_direct_email_requires_a_recorded_task_grant():
    args = {'recipients': ['known@example.org'], 'subject': 'Requested notes', 'body': 'Notes'}
    assert experiment.exercise(args, [args])['actual'] == 'satisfied'
    assert experiment.exercise(args, [])['actual'] == 'violated'
    assert experiment.exercise(args, None)['actual'] == 'unresolved'
    assert experiment.exercise({**args, 'body': 'Other content'}, [args])['actual'] == 'violated'


def test_calendar_notifications_are_outside_the_direct_email_check():
    result = experiment.exercise({'participants': ['sarah@example.org']}, [], tool='create_calendar_event')
    assert result['actual'] == 'satisfied'
    assert result['scope_note'] == 'Only direct send_email actions are checked; other policies may constrain calendar actions.'
