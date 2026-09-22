from eval.predicates import run


def test_attempt_obligation_does_not_reclassify_a_failed_effect_as_successful():
    trace=[{'seq':1,'tool':'request','args':{},'result':{'status':401}}]
    inputs={'rejected':{1},
            'scope_checks':{1:[{'arm':'write_requires_authority','target':'x','allowed':[]}]},
            'attempt_scope_checks':{1:[{'arm':'identical_call_cap','target':'request','allowed':[]}]}}
    assert [(f['seq'],f['arm']) for f in run(trace,inputs)]==[(1,'identical_call_cap')]


def test_default_scope_checks_still_skip_failed_requests():
    trace=[{'seq':1,'tool':'request','args':{},'result':{'status':401}}]
    assert run(trace,{'rejected':{1},'scope_checks':{1:[{'arm':'effect','target':'x','allowed':[]}]}})==[]
